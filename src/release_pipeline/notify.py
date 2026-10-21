"""Telegram notifications. Dry-run unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
import os
import re
import urllib.parse
import urllib.request


def redact(text: str, token: str | None = None) -> str:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if token:
        text = text.replace(token, "***")
    return re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "***", text)  # bot-token shape


def send(message: str) -> bool:
    message = redact(message)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(f"[notify dry-run] {message}")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10)
        return True
    except Exception as exc:  # never let a failed alert break the pipeline
        print(f"[notify failed] {redact(str(exc), token)}")
        return False
