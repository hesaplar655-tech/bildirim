"""Telegram'a mesaj gonderen script.

Gerekli ortam degiskenleri:
  TELEGRAM_TOKEN    - BotFather'dan alinan token (GitHub Secrets'ta saklanir)
  TELEGRAM_CHAT_ID  - Mesajin gidecegi sohbetin id'si
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


def build_message() -> str:
    """Gonderilecek mesaji burada olustur. Simdilik test mesaji."""
    tr_now = datetime.now(timezone.utc) + timedelta(hours=3)
    return f"✅ Bildirim sistemi calisiyor.\nZaman: {tr_now:%d.%m.%Y %H:%M} (TR)"


def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram hatasi: {result}")


def main() -> int:
    try:
        send_telegram(build_message())
    except Exception as exc:  # GitHub'da kirmizi X olarak gorunsun
        print(f"Gonderilemedi: {exc}", file=sys.stderr)
        return 1
    print("Mesaj gonderildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
