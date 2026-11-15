// Haber botu - Telegram numara cevaplari (Cloudflare Worker)
//
// Telegram, bota yazilan her mesaji aninda buraya iletir (webhook).
// Numaralar, GitHub'daki bot.py'nin her sabah yazdigi digest.json'dan okunur.
//
// Gizli ayarlar (wrangler secret): TELEGRAM_TOKEN, WEBHOOK_SECRET
// Normal ayarlar (wrangler.toml): CHAT_ID, DIGEST_URL

const TG_LIMIT = 4000;
const MAX_NUMBERS = 10;
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

// Uyelik istemeyen siteler: metin dogrudan cekilir. Digerleri archive.is linkiyle verilir.
const ARTICLE_SELECTORS = {
  CNBC: ["div.ArticleBody-articleBody h2", "div.ArticleBody-articleBody p"],
  "New York Post": ["div.single__content p"],
};

const HELP =
  "Her sabah 09:00'da numaralı haber listesi gelir.\n" +
  "Bir haberi okumak için numarasını yaz: 5\n" +
  "Birden fazla için: 3, 12, 40\n" +
  "Numaralar en son gönderilen listeye aittir.";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Telegram'dan gelen mesajlar
    if (request.method === "POST" && url.pathname === "/hook") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await request.json();
      ctx.waitUntil(handleUpdate(update, env).catch((e) => console.error("hata:", e)));
      return new Response("ok");
    }

    // Telegram'i bu Worker'a baglamak icin tek seferlik kurulum
    if (url.pathname === "/setup") {
      if (url.searchParams.get("key") !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const res = await tg(env, "setWebhook", {
        url: `${url.origin}/hook`,
        secret_token: env.WEBHOOK_SECRET,
        allowed_updates: ["message"],
      });
      return new Response(JSON.stringify(await res.json()), {
        headers: { "content-type": "application/json" },
      });
    }

    // Yerel test: haber metni cikarma (sadece DEBUG ayari varken)
    if (env.DEBUG && url.pathname === "/debug") {
      const text = await fetchArticle(url.searchParams.get("url"), url.searchParams.get("source"));
      return new Response(text ?? "(metin yok)", { headers: { "content-type": "text/plain; charset=utf-8" } });
    }

    return new Response("haber-botu calisiyor");
  },
};

// ---------------------------------------------------------------- telegram

function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function chunks(text, limit = TG_LIMIT) {
  const out = [];
  let cur = "";
  for (const line of text.split("\n")) {
    const pieces = line.length ? line.match(new RegExp(`[\\s\\S]{1,${limit}}`, "g")) : [""];
    for (const piece of pieces) {
      if (cur && cur.length + piece.length + 1 > limit) {
        out.push(cur);
        cur = piece;
      } else {
        cur = cur ? `${cur}\n${piece}` : piece;
      }
    }
  }
  if (cur.trim()) out.push(cur);
  return out;
}

async function send(env, text) {
  for (const part of chunks(text)) {
    const body = { chat_id: env.CHAT_ID, text: part, link_preview_options: { is_disabled: true } };
    for (let i = 0; i < 3; i++) {
      const res = await tg(env, "sendMessage", body);
      if (res.status !== 429) break;
      const wait = ((await res.json()).parameters?.retry_after ?? 3) + 1;
      await new Promise((r) => setTimeout(r, wait * 1000));
    }
  }
}

// ---------------------------------------------------------------- mesaj isleme

async function handleUpdate(update, env) {
  const msg = update.message;
  if (!msg || String(msg.chat?.id) !== String(env.CHAT_ID)) return; // baskasi yazmis

  const nums = [...new Set((msg.text || "").match(/\d+/g) || [])].map((n) => String(Number(n)));
  if (!nums.length) return send(env, HELP);

  await tg(env, "sendChatAction", { chat_id: env.CHAT_ID, action: "typing" });

  const res = await fetch(`${env.DIGEST_URL}?t=${Date.now()}`, { cf: { cacheTtl: 0 } });
  const items = res.ok ? (await res.json()).items || {} : {};
  const total = Object.keys(items).length;
  if (!total) return send(env, "Henüz gönderilmiş bir liste yok.");

  for (const num of nums.slice(0, MAX_NUMBERS)) {
    const it = items[num];
    if (!it) {
      await send(env, `${num} numaralı haber son listede yok (1-${total} arası yazabilirsin).`);
      continue;
    }
    await send(env, await articleMessage(num, it));
  }
}

function formatTR(iso) {
  const d = new Date(new Date(iso).getTime() + 3 * 3600 * 1000); // Turkiye UTC+3
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCDate())}.${p(d.getUTCMonth() + 1)} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

const archiveLink = (url) => "archive.is/" + url.split("?")[0];

async function articleMessage(num, it) {
  const when = it.published ? ` · ${formatTR(it.published)}` : "";
  const head = [`[${num}] ${it.source}${when}`, it.title, ""];

  // Substack: metin listeyle birlikte kaydedildi
  if (it.content) {
    return [
      ...head,
      "📝 Özet", it.desc || "(özet yok)", "",
      "📄 Yazı", it.content, "",
      "(Ücretli yazılarda burada sadece ilk kısım var, tamamı linkte.)",
      `🔗 ${it.url}`,
    ].join("\n");
  }

  if (ARTICLE_SELECTORS[it.source]) {
    const body = await fetchArticle(it.url, it.source);
    const summary = it.desc || (body ? body.split("\n")[0] : "");
    const parts = [...head, "📝 Özet", summary || "(özet yok)", ""];
    if (body) parts.push("📄 Haber", body, "", `🔗 ${it.url}`);
    else parts.push("Haber metni alınamadı, linkten okuyabilirsin:", `🔗 ${it.url}`);
    return parts.join("\n");
  }

  const parts = [...head, "📝 Özet", it.desc || "(bu kaynak akışında özet yok)", ""];
  if (it.url.includes("news.google.com")) {
    parts.push(`🔗 ${it.url}`); // gercek adres cozulemediyse
  } else {
    parts.push(`🔒 Tam haber (archive): ${archiveLink(it.url)}`, `🔗 Orijinal: ${it.url}`);
  }
  return parts.join("\n");
}

// ---------------------------------------------------------------- haber metni

const ENTITIES = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ",
  rsquo: "’", lsquo: "‘", rdquo: "”", ldquo: "“", mdash: "—", ndash: "–", hellip: "…",
};

function decodeEntities(s) {
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === "#") {
      const code = e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return ENTITIES[e.toLowerCase()] ?? m;
  });
}

async function fetchArticle(url, source) {
  const selectors = ARTICLE_SELECTORS[source];
  if (!url || !selectors) return null;
  try {
    const res = await fetch(url, { headers: { "User-Agent": UA } });
    if (!res.ok) return null;

    const paras = [];
    let cur = null;
    const handler = {
      element(el) {
        cur = "";
        el.onEndTag(() => {
          const t = decodeEntities(cur || "").replace(/\s+/g, " ").trim();
          if (t && !/^advertisement$/i.test(t)) paras.push(t);
          cur = null;
        });
      },
      text(chunk) {
        if (cur !== null) cur += chunk.text;
      },
    };
    let rw = new HTMLRewriter();
    for (const sel of selectors) rw = rw.on(sel, handler);
    await rw.transform(res).arrayBuffer();

    return paras.length ? paras.join("\n") : null;
  } catch (e) {
    console.error("haber cekilemedi:", url, e);
    return null;
  }
}
