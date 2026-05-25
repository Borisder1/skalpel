import os
import logging
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
logger = logging.getLogger(__name__)
_LAST_UPDATE_ID = 0

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


def send_position_opened(token, chat_id, pos):
    symbol = pos["symbol"].replace("/USDT:USDT", "")
    emoji = "🟢" if pos["side"] == "Buy" else "🔴"
    side = "LONG" if pos["side"] == "Buy" else "SHORT"
    text = (
        f"✅ *ПОЗИЦІЯ ВІДКРИТА*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{emoji} *{side} | {symbol}*\n"
        f"📍 Ціна входу: `{pos['entry_price']}`\n"
        f"📦 Розмір: `{pos['qty']}`\n"
        f"🛡 SL: `{pos['sl']}`\n"
        f"🎯 TP: `{pos['tp']}`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now().strftime('%H:%M %d\\.%m')}"
    )
    return requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
        timeout=10,
    )


def send_position_closed(token, chat_id, pos):
    symbol = pos["symbol"].replace("/USDT:USDT", "")
    pnl = float(pos.get("pnl", 0))
    pnl_emoji = "💰" if pnl > 0 else "💸"
    pnl_sign = "+" if pnl > 0 else ""
    text = (
        f"{pnl_emoji} *ПОЗИЦІЯ ЗАКРИТА*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"*{symbol}*\n"
        f"📍 Вхід:  `{pos['entry_price']}`\n"
        f"📍 Вихід: `{pos['exit_price']}`\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 PnL: *{pnl_sign}{pnl:.4f} USDT*\n"
        f"🕐 {datetime.now().strftime('%H:%M %d\\.%m')}"
    )
    return requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
        timeout=10,
    )


def send_signal_with_buttons(token, chat_id, signal):
    signal_id = signal["id"]
    direction = signal["direction"]
    symbol = signal["symbol"].replace("/USDT:USDT", "").replace("/USDT", "")
    emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    text = f"⚡ *{emoji} | {symbol}*\nПідтвердити відкриття позиції?"
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Відкрити позицію", "callback_data": f"confirm_{signal_id}"},
            {"text": "❌ Пропустити", "callback_data": f"skip_{signal_id}"}
        ]]
    }
    return requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2", "reply_markup": keyboard},
        timeout=10,
    )


def answer_callback(token, callback_id, text):
    return requests.post(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text},
        timeout=10,
    )


def poll_telegram_callbacks(token, pending_signals):
    global _LAST_UPDATE_ID
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"offset": _LAST_UPDATE_ID, "timeout": 1},
        timeout=10,
    ).json()
    for update in resp.get("result", []):
        _LAST_UPDATE_ID = update["update_id"] + 1
        callback = update.get("callback_query")
        if not callback:
            continue
        data = callback.get("data", "")
        if data.startswith("confirm_"):
            signal_id = data.replace("confirm_", "")
            sig = pending_signals.get(signal_id)
            if sig:
                sig["approved"] = True
                answer_callback(token, callback["id"], "✅ Ордер відкрито!")
        elif data.startswith("skip_"):
            signal_id = data.replace("skip_", "")
            if signal_id in pending_signals:
                pending_signals[signal_id]["approved"] = False
            answer_callback(token, callback["id"], "❌ Пропущено")


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
