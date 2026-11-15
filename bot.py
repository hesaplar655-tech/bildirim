"""Haber botu - toplama ve gunluk liste.

Her calistiginda (GitHub Actions, ~5 dakikada bir):
  1. Haber akislarini (RSS) toplar, yeni basliklari state/pending.json'a ekler.
  2. Saat 09:00 (TR) gectiyse ve bugun gonderilmediyse numarali baslik listesini Telegram'a yollar
     ve state/digest.json'a yazar.

Telegram'a yazilan numaralara Cloudflare Worker (worker/ klasoru) aninda cevap verir;
numaralari bu dosyanin yazdigi digest.json'dan okur.

Ortam degiskenleri: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

TR = timezone(timedelta(hours=3))
DIGEST_HOUR = 9
STATE_DIR = Path("state")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
TG_LIMIT = 4000

GN = "https://news.google.com/rss/search?q=site:reuters.com/{}+when:1d&hl=en-US&gl=US&ceid=US:en"
CNBC = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id={}"
NYT = "https://rss.nytimes.com/services/xml/rss/nyt/{}.xml"

# (kaynak adi, akis adresi). Siralama, listedeki siralamayi belirler.
FEEDS = [
    ("Reuters", GN.format("business")),
    ("Reuters", GN.format("markets")),
    ("Reuters", GN.format("technology")),
    ("Bloomberg", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Bloomberg", "https://feeds.bloomberg.com/business/news.rss"),
    ("Bloomberg", "https://feeds.bloomberg.com/technology/news.rss"),
    ("Financial Times", "https://www.ft.com/news-feed?format=rss"),
    ("The Economist", "https://www.economist.com/latest/rss.xml"),
    ("New York Times", NYT.format("Business")),
    ("New York Times", NYT.format("Economy")),
    ("New York Times", NYT.format("Technology")),
    ("CNBC", CNBC.format("10001147")),  # Business
    ("CNBC", CNBC.format("10000664")),  # Finance
    ("CNBC", CNBC.format("15839069")),  # Investing
    ("CNBC", CNBC.format("20910258")),  # Economy
    ("CNBC", CNBC.format("19854910")),  # Tech
    ("New York Post", "https://nypost.com/business/feed/"),
    ("New York Post", "https://nypost.com/tech/feed/"),
    # Substack: yazinin metni akistan alinir (ucretli yazilarda sadece ilk kisim)
    ("Global Macro Method", "https://globalmacromethod.substack.com/feed"),
    ("Bison Insights", "https://bisoninvest.substack.com/feed"),
    ("Campbell Ramble", "https://campbellramble.substack.com/feed"),
]
SUBSTACKS = {"Global Macro Method", "Bison Insights", "Campbell Ramble"}
SOURCE_ORDER = list(dict.fromkeys(name for name, _ in FEEDS))


# ---------------------------------------------------------------- yardimcilar

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load(name: str, default):
    path = STATE_DIR / name
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save(name: str, data) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def html_to_text(s: str) -> str:
    """Yazi HTML'ini paragraflari koruyarak duz metne cevirir."""
    s = re.sub(r"(?is)<(script|style|figure|figcaption)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|h[1-6]|li|blockquote|div)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in s.split("\n")]
    boilerplate = {"subscribe now", "share", "leave a comment", "upgrade to paid", "subscribe"}
    return "\n".join(
        ln for ln in lines
        if ln and ln.lower() not in boilerplate and "reader-supported publication" not in ln.lower()
    )


def chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Metni Telegram sinirina gore satir satir boler."""
    out, cur = [], ""
    for line in text.split("\n"):
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            if cur and len(cur) + len(piece) + 1 > limit:
                out.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n{piece}" if cur else piece
    if cur.strip():
        out.append(cur)
    return out


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)

    def send(self, text: str) -> None:
        for part in chunks(text):
            payload = {"chat_id": self.chat_id, "text": part, "link_preview_options": {"is_disabled": True}}
            for _ in range(3):
                r = requests.post(f"{self.base}/sendMessage", json=payload, timeout=30)
                if r.status_code != 429:  # 429 = hiz siniri, bekleyip tekrar dene
                    break
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            r.raise_for_status()
            time.sleep(0.4)


# ---------------------------------------------------------------- akis toplama

def fetch_feed(source: str, url: str) -> list[dict]:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    items = []
    for e in parsed.entries:
        title = clean_text(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        desc = clean_text(e.get("summary", ""))
        if source == "Reuters":
            title = re.sub(r"\s+-\s+Reuters$", "", title)
            desc = ""  # Google Haberler aciklamasi sadece link icerir
            if "Stock Price" in title or "| Reuters" in title:
                continue
        pub = e.get("published_parsed") or e.get("updated_parsed")
        pub_iso = datetime(*pub[:6], tzinfo=timezone.utc).isoformat() if pub else None
        item = {"source": source, "title": title, "url": link, "desc": desc, "published": pub_iso}
        if source in SUBSTACKS and e.get("content"):
            item["content"] = html_to_text(e["content"][0].get("value", ""))
        items.append(item)
    return items


def decode_google_link(url: str) -> str:
    """Google Haberler yonlendirme linkini gercek Reuters adresine cevirir (archive linki icin)."""
    try:
        from googlenewsdecoder import gnewsdecoder

        res = gnewsdecoder(url, interval=1)
        if res.get("decoded_url"):
            return res["decoded_url"]
    except Exception as exc:
        print(f"[uyari] Google link cozulemedi: {exc}", file=sys.stderr)
    return url


def collect(state: dict, pending: list[dict]) -> list[dict]:
    seen: dict = state.setdefault("seen", {})
    last_digest = datetime.fromisoformat(state["last_digest_at"])
    cutoff = last_digest - timedelta(hours=6)  # gec dusen haberler icin pay
    known_titles = {(p["source"], p["title"].lower()) for p in pending}
    added = 0
    for source, url in FEEDS:
        try:
            items = fetch_feed(source, url)
        except Exception as exc:  # bir akis bozuksa digerleri devam etsin
            print(f"[uyari] {source} akisi alinamadi: {exc}", file=sys.stderr)
            continue
        for it in items:
            key = it["url"].split("?", 1)[0]
            if key in seen or (it["source"], it["title"].lower()) in known_titles:
                continue
            if it["published"] and datetime.fromisoformat(it["published"]) < cutoff:
                continue
            seen[key] = now_utc().isoformat()
            known_titles.add((it["source"], it["title"].lower()))
            if "news.google.com" in it["url"]:
                it["url"] = decode_google_link(it["url"])
            pending.append(it)
            added += 1
    # 5 gunden eski 'seen' kayitlarini sil
    limit = now_utc() - timedelta(days=5)
    state["seen"] = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > limit}
    state["last_fetch"] = now_utc().isoformat()
    print(f"{added} yeni haber eklendi, bekleyen: {len(pending)}")
    return pending


# ---------------------------------------------------------------- gunluk liste

def send_digest(tg: Telegram, state: dict, pending: list[dict]) -> None:
    start = datetime.fromisoformat(state["last_digest_at"]).astimezone(TR)
    end = now_utc().astimezone(TR)
    by_source: dict[str, list[dict]] = {s: [] for s in SOURCE_ORDER}
    for it in pending:
        by_source.setdefault(it["source"], []).append(it)

    numbered: dict[str, dict] = {}
    lines = [
        f"📰 Haber listesi · {end:%d.%m.%Y %H:%M}",
        f"{start:%d.%m %H:%M} → {end:%d.%m %H:%M} arası · {len(pending)} haber",
        "",
    ]
    n = 0
    for source in SOURCE_ORDER:
        group = sorted(by_source.get(source, []), key=lambda x: x["published"] or "", reverse=True)
        if not group:
            continue
        lines.append(f"━━ {source} ({len(group)}) ━━")
        for it in group:
            n += 1
            numbered[str(n)] = it
            lines.append(f"{n}. {it['title']}")
        lines.append("")
    if n == 0:
        lines.append("Bu aralıkta yeni haber yok.")
    else:
        lines.append("Haberi okumak için numarasını yaz (örn: 5 ya da 3 12 40).")

    tg.send("\n".join(lines))
    save("digest.json", {"sent_at": end.isoformat(), "items": numbered})
    state["last_digest_at"] = now_utc().isoformat()
    state["last_digest_date"] = end.date().isoformat()
    pending.clear()
    print(f"Liste gonderildi: {n} haber")


# ---------------------------------------------------------------- ana akis

def main() -> int:
    tg = Telegram(os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    state = load("state.json", {})
    state.setdefault("last_digest_at", (now_utc() - timedelta(hours=24)).isoformat())
    pending = load("pending.json", [])

    # 1) Akislari topla
    pending = collect(state, pending)

    # 2) Gunluk liste zamani geldiyse gonder
    now_tr = now_utc().astimezone(TR)
    if now_tr.hour >= DIGEST_HOUR and state.get("last_digest_date") != now_tr.date().isoformat():
        send_digest(tg, state, pending)

    save("state.json", state)
    save("pending.json", pending)
    return 0


if __name__ == "__main__":
    sys.exit(main())
