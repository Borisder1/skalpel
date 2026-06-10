import os
import logging
import re
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
logger = logging.getLogger(__name__)
_LAST_UPDATE_ID = 0
_PROCESSED_CALLBACKS = set()


def _sanitize_secret(text: object) -> str:
    text = str(text)
    text = re.sub(r"/bot[^/]+/", "/bot***/", text)
    text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", text)
    return text

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

    quant_score = signal.get("quant_score") or signal.get("ai_confidence")
    quant_line = ""
    if quant_score is not None:
        quant_line = f"🧠 Оцінка: *{quant_score * 100:.0f}%*\n"
        rat = signal.get("ai_rationale")
        if rat:
            clean_rat = str(rat).replace("*", "").replace("_", "").replace("`", "")
            quant_line += f"📝 Деталі: _{clean_rat}_\n"

    text = (
        f"⚡ *{emoji} | {symbol}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 Вхід:  *{fp(signal['entry'])}*\n"
        f"🛡 SL:    *{fp(signal['sl'])}*\n"
        f"🎯 TP1:  *{fp(signal['tp1'])}*\n"
        f"🎯 TP2:  *{fp(signal['tp2'])}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 R:R = 1:{rr} | ATR={atr_str}\n"
        f"{quant_line}"
        f"🕐 {datetime.now().strftime('%H:%M %d.%m')}"
    )

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
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
        f"📍 Ціна входу: *{pos['entry_price']}*\n"
        f"📦 Розмір: *{pos['qty']}*\n"
        f"🛡 SL: *{pos['sl']}*\n"
        f"🎯 TP: *{pos['tp']}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now().strftime('%H:%M %d.%m')}"
    )
    return requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
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
        f"📍 Вхід:  *{pos['entry_price']}*\n"
        f"📍 Вихід: *{pos['exit_price']}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 PnL: *{pnl_sign}{pnl:.4f} USDT*\n"
        f"🕐 {datetime.now().strftime('%H:%M %d.%m')}"
    )
    return requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )


def send_signal_with_buttons(token, chat_id, signal):
    signal_id = signal["id"]
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
        return f"{p:.8f}"

    atr = float(signal.get("atr", 0) or 0)
    atr_str = f"{atr:.6f}" if atr < 0.001 else f"{atr:.4f}"
    quant_score = signal.get("quant_score") or signal.get("ai_confidence")
    quant_line = ""
    if quant_score is not None:
        quant_line = f"🧠 Оцінка: *{quant_score * 100:.0f}%*\n"
        rat = signal.get("ai_rationale")
        if rat:
            clean_rat = str(rat).replace("*", "").replace("_", "").replace("`", "")
            quant_line += f"📝 Деталі: _{clean_rat}_\n"

    text = (
        f"⚡ *{emoji} | {symbol}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 Вхід: *{fp(signal['entry'])}*\n"
        f"🛡 SL: *{fp(signal['sl'])}*\n"
        f"🎯 TP1: *{fp(signal['tp1'])}*\n"
        f"🎯 TP2: *{fp(signal['tp2'])}*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 ATR={atr_str}\n"
        f"{quant_line}"
        f"Підтвердити відкриття позиції?"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Відкрити позицію", "callback_data": f"confirm_{signal_id}"},
            {"text": "❌ Пропустити", "callback_data": f"skip_{signal_id}"}
        ]]
    }
    return requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard},
        timeout=10,
    )


def answer_callback(token, callback_id, text):
    return requests.post(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text},
        timeout=10,
    )


def update_config_value(key: str, value) -> bool:
    import json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            config[key] = value
            temp = config_path + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
            os.replace(temp, config_path)
            return True
        except Exception as e:
            logger.error("Error updating config: %s", e)
    return False


def get_current_settings_str() -> str:
    import json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return (
                f"⚙️ *Поточні налаштування SMC Racer*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Ризик на угоду: *{config.get('risk_pct')}%*\n"
                f"🛡 Макс відкритих позицій: *{config.get('max_concurrent_positions')}*\n"
                f"🛡 Макс активних ордерів: *{config.get('max_active_orders')}*\n"
                f"🎯 TP1 R:R: *{config.get('tp1_rr')}*\n"
                f"🎯 TP2 R:R: *{config.get('tp2_rr')}*\n"
                f"🧠 Оцінка авто-входу: *{config.get('auto_execute_confidence_threshold') * 100:.0f}%*\n"
                f"🔔 Потрібне підтвердження: *{config.get('require_confirmation')}*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"💡 _Для зміни параметрів відправте команду:_\n"
                f"`/set_risk <значення>` (напр. `/set_risk 1.0`)\n"
                f"`/set_positions <число>` (напр. `/set_positions 25`)\n"
                f"`/set_orders <число>` (напр. `/set_orders 25`)\n"
                f"`/set_tp1 <значення>` (напр. `/set_tp1 1.5`)\n"
                f"`/set_tp2 <значення>` (напр. `/set_tp2 3.0`)\n"
                f"`/set_auto <значення>` (напр. `/set_auto 0.65`)\n"
                f"`/set_confirm <true/false>`"
            )
        except Exception as e:
            logger.error("Error reading config: %s", e)
    return "❌ Не вдалося прочитати файл налаштувань."


def poll_telegram_callbacks(token, pending_signals):
    global _LAST_UPDATE_ID
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": _LAST_UPDATE_ID, "timeout": 1},
            timeout=10,
        ).json()
    except Exception as e:
        logger.warning("TG callbacks poll error: %s", _sanitize_secret(e))
        return False
        
    for update in resp.get("result", []):
        _LAST_UPDATE_ID = update["update_id"] + 1
        
        # 1. Обробка натискання на інлайн-кнопки
        callback = update.get("callback_query")
        if callback:
            cb_id = callback.get("id")
            if cb_id in _PROCESSED_CALLBACKS:
                continue
            if cb_id:
                _PROCESSED_CALLBACKS.add(cb_id)
            data = callback.get("data", "")
            if data.startswith("confirm_"):
                signal_id = data.replace("confirm_", "")
                sig = pending_signals.get(signal_id)
                if sig:
                    sig["approved"] = True
                    answer_callback(token, callback["id"], "✅ Ордер відкрито!")
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={
                            "chat_id": callback["message"]["chat"]["id"],
                            "text": f"✅ Підтверджено: {sig['signal']['direction']} {sig['signal']['symbol']}",
                            "parse_mode": "Markdown",
                        },
                        timeout=10,
                    )
            elif data.startswith("skip_"):
                signal_id = data.replace("skip_", "")
                if signal_id in pending_signals:
                    pending_signals[signal_id]["approved"] = False
                answer_callback(token, callback["id"], "❌ Пропущено")
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": callback["message"]["chat"]["id"],
                        "text": f"❌ Скасовано: {signal_id}",
                        "parse_mode": "Markdown",
                    },
                    timeout=10,
                )
                
        # 2. Обробка текстових повідомлень (налаштування)
        msg = update.get("message")
        if msg:
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "").strip()
            if not text:
                continue
                
            if text == "/settings":
                settings_text = get_current_settings_str()
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": settings_text,
                        "parse_mode": "Markdown"
                    },
                    timeout=10
                )
            elif text.startswith("/set_"):
                parts = text.split(" ")
                if len(parts) == 2:
                    cmd = parts[0]
                    val_str = parts[1]
                    
                    key_map = {
                        "/set_risk": ("risk_pct", float),
                        "/set_positions": ("max_concurrent_positions", int),
                        "/set_orders": ("max_active_orders", int),
                        "/set_tp1": ("tp1_rr", float),
                        "/set_tp2": ("tp2_rr", float),
                        "/set_auto": ("auto_execute_confidence_threshold", float),
                        "/set_confirm": ("require_confirmation", lambda x: x.lower() == "true")
                    }
                    
                    if cmd in key_map:
                        key, cast_func = key_map[cmd]
                        try:
                            val = cast_func(val_str)
                            if update_config_value(key, val):
                                resp_text = f"✅ Налаштування *{key}* успішно змінено на *{val}*!"
                            else:
                                resp_text = "❌ Помилка оновлення файлу конфігурації."
                        except Exception:
                            resp_text = f"❌ Невірний формат значення для {cmd}."
                    else:
                        resp_text = f"❌ Невідома команда {cmd}."
                else:
                    resp_text = "❌ Формат команди невірний. Спробуй: `/set_risk 1.0`"
                    
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": resp_text,
                        "parse_mode": "Markdown"
                    },
                    timeout=10
                )


def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Немає токена або ID для Telegram. Повідомлення не відправлено.")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if response.status_code != 200:
            # Fallback: якщо HTML не парситься — відправляємо без форматування
            resp_json = response.json() if response.text else {}
            if "can't parse entities" in str(resp_json.get("description", "")):
                import html as html_mod
                clean_msg = re.sub(r"<[^>]+>", "", message)  # strip all HTML tags
                response = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": clean_msg},
                    timeout=10
                )
                if response.status_code != 200:
                    print(f"⚠️ Помилка відправки в ТГ (fallback): {response.text}")
            else:
                print(f"⚠️ Помилка відправки в ТГ: {response.text}")
    except Exception as e:
        print(f"⚠️ Помилка мережі при відправці в ТГ: {_sanitize_secret(e)}")

if __name__ == "__main__":
    send_telegram_message("🤖 <b>SMC Racer</b>\nМодуль Telegram успішно підключено!")
