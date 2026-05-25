import os
import html
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(message: str):
    """Відправляє повідомлення в Телеграм через Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Немає токена або ID для Telegram. Повідомлення не відправлено.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": html.escape(message),
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"⚠️ Помилка відправки в ТГ: {response.text}")
    except Exception as e:
        print(f"⚠️ Помилка мережі при відправці в ТГ: {e}")

if __name__ == "__main__":
    send_telegram_message("🤖 <b>SMC Racer</b>\nМодуль Telegram успішно підключено!")
