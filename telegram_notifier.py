import os
import logging
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
logger = logging.getLogger(__name__)

def send_signal(token, chat_id, signal):
    direction = signal["direction"]
    symbol = signal["symbol"].replace("/USDT:USDT", "").replace("/USDT", "")
    emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"

    def fp(p):
        p = float(p)
        if p >= 1:
            return f"{p:.4f}"
        elif p >= 0.01:
            return f"{p:.5f}"
        elif p >= 0.001:
            return f"{p:.6f}"
        else:
            return f"{p:.8f}"

    atr = float(signal.get("atr", 0) or 0)
    atr_str = f"{atr:.6f}" if atr < 0.001 else f"{atr:.4f}"
    rr = signal.get("rr", 1.5)

    text = (
        f"⚡ *{emoji} | {symbol}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 Вхід:  `{fp(signal['entry'])}`\n"
        f"🛡 SL:    `{fp(signal['sl'])}`\n"
        f"🎯 TP1:  `{fp(signal['tp1'])}`\n"
        f"🎯 TP2:  `{fp(signal['tp2'])}`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 R:R = 1:{rr} \\| ATR={atr_str}\n"
        f"🕐 {datetime.now().strftime('%H:%M %d\\.%m')}"
    )

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
        timeout=10,
    )
    try:
        j = resp.json()
    except Exception:
        j = {"ok": False, "raw": resp.text}
    if not j.get("ok"):
        logger.warning(f"TG помилка: {j}")
    return resp


def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Немає токена або ID для Telegram. Повідомлення не відправлено.")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "MarkdownV2"},
            timeout=10
        )
        if response.status_code != 200:
            print(f"⚠️ Помилка відправки в ТГ: {response.text}")
    except Exception as e:
        print(f"⚠️ Помилка мережі при відправці в ТГ: {e}")

if __name__ == "__main__":
    send_telegram_message("🤖 <b>SMC Racer</b>\nМодуль Telegram успішно підключено!")
