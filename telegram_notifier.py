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


_USER_STATES = {}  # {chat_id: state_name}


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


def send_settings_menu(token, chat_id, message_id=None):
    import json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_config.json")
    if not os.path.exists(config_path):
        return
        
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error("Error reading config for menu: %s", e)
        return

    text = (
        f"⚙️ *Налаштування SMC Racer*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Ризик на угоду: *{config.get('risk_pct')}%*\n"
        f"🛡 Макс відкритих позицій: *{config.get('max_concurrent_positions')}*\n"
        f"🛡 Макс активних ордерів: *{config.get('max_active_orders')}*\n"
        f"🎯 TP1 R:R: *{config.get('tp1_rr')}*\n"
        f"🎯 TP2 R:R: *{config.get('tp2_rr')}*\n"
        f"🧠 Поріг авто-входу: *{config.get('auto_execute_confidence_threshold') * 100:.0f}%*\n"
        f"🔔 Потрібне підтвердження: *{config.get('require_confirmation')}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Оберіть параметр для зміни:"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "💰 Ризик на угоду", "callback_data": "menu_risk_pct"},
                {"text": "🛡 Ліміт позицій", "callback_data": "menu_max_concurrent_positions"}
            ],
            [
                {"text": "🎯 TP1 R:R", "callback_data": "menu_tp1_rr"},
                {"text": "🎯 TP2 R:R", "callback_data": "menu_tp2_rr"}
            ],
            [
                {"text": "🧠 Поріг авто-входу", "callback_data": "menu_auto_execute_confidence_threshold"},
                {"text": "🔔 Підтвердження (On/Off)", "callback_data": "menu_toggle_require_confirmation"}
            ]
        ]
    }

    if message_id:
        requests.post(
            f"https://api.telegram.org/bot{token}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            },
            timeout=10
        )
    else:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            },
            timeout=10
        )

def send_main_menu(token, chat_id, message_id=None):
    # V10: Live dashboard з реальними даними
    try:
        import pnl_tracker
        import db_logger
        summary_data = pnl_tracker.load_stats()
        trades_list = summary_data.get("trades", [])
        total_pnl = sum(float(t.get("pnl", 0)) for t in trades_list)
        wins = sum(1 for t in trades_list if float(t.get("pnl", 0)) > 0)
        total = len(trades_list)
        wr = (wins / total * 100) if total > 0 else 0
        open_trades = db_logger.get_open_trades()
        active_bans = db_logger.get_active_blacklist()
        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        header = (
            f"🤖 *SMC Racer v10 Dashboard*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{pnl_emoji} PnL: *{pnl_sign}{total_pnl:.2f} USDT*\n"
            f"📊 Угод: *{total}* | WR: *{wr:.1f}%*\n"
            f"🟢 Відкритих: *{len(open_trades)}*\n"
            f"🚫 Банів: *{len(active_bans)}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Оберіть дію:"
        )
    except Exception:
        header = (
            f"🤖 *SMC Racer v10 Dashboard*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Оберіть дію:"
        )
    
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📊 Статистика та PnL", "callback_data": "menu_main_stats"},
                {"text": "🟢 Активні Угоди", "callback_data": "menu_main_trades"}
            ],
            [
                {"text": "⚙️ Налаштування", "callback_data": "menu_main_settings"},
                {"text": "🧠 AI Звіт", "callback_data": "menu_main_ai_report"}
            ],
            [
                {"text": "🚫 Blacklist", "callback_data": "menu_main_blacklist"},
                {"text": "🌡️ Режим Ринку", "callback_data": "menu_main_regime"}
            ]
        ]
    }
    if message_id:
        requests.post(
            f"https://api.telegram.org/bot{token}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": header, "parse_mode": "Markdown", "reply_markup": keyboard},
            timeout=10
        )
    else:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": header, "parse_mode": "Markdown", "reply_markup": keyboard},
            timeout=10
        )
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

        # 1. Обробка inline кнопок
        callback = update.get("callback_query")
        if callback:
            cb_id = callback.get("id")
            if cb_id in _PROCESSED_CALLBACKS:
                continue
            if cb_id:
                _PROCESSED_CALLBACKS.add(cb_id)
            data = callback.get("data", "")
            chat_id = callback["message"]["chat"]["id"]
            msg_id = callback["message"]["message_id"]

            if data.startswith("confirm_"):
                signal_id = data.replace("confirm_", "")
                sig = pending_signals.get(signal_id)
                if sig:
                    sig["approved"] = True
                    answer_callback(token, callback["id"], "✅ Ордер відкрито!")
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={
                            "chat_id": chat_id,
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
                        "chat_id": chat_id,
                        "text": f"❌ Скасовано: {signal_id}",
                        "parse_mode": "Markdown",
                    },
                    timeout=10,
                )
            elif data == "menu_toggle_require_confirmation":
                # Завантажуємо поточне, змінюємо на протилежне
                import json
                config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_config.json")
                req_conf = False
                if os.path.exists(config_path):
                    try:
                        with open(config_path, "r") as f:
                            cfg = json.load(f)
                        req_conf = not cfg.get("require_confirmation", False)
                        update_config_value("require_confirmation", req_conf)
                    except Exception:
                        pass
                
                answer_callback(token, callback["id"], f"🔔 Режим підтвердження: {req_conf}")
                send_settings_menu(token, chat_id, message_id=msg_id)
            elif data == "menu_main_settings":
                send_settings_menu(token, chat_id, message_id=msg_id)
            elif data == "menu_main_stats":
                answer_callback(token, callback["id"], "Оновлюю статистику...")
                import pnl_tracker
                text = pnl_tracker.get_summary()
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10
                )
            elif data == "menu_main_trades":
                answer_callback(token, callback["id"], "Завантажую...")
                import db_logger
                trades = db_logger.get_open_trades()
                text = f"🟢 *Активні угоди ({len(trades)}):*\n"
                for t in trades:
                    text += f"• {t['symbol']} | {t['direction']} | {t['status']}\n"
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10
                )
            elif data == "menu_main_ai_report":
                answer_callback(token, callback["id"], "Отримую пам'ять ШІ...")
                import db_logger
                mem = db_logger.get_latest_ai_memory()
                if mem:
                    text = f"🧠 *Persistent AI Memory*\nОновлено: {mem['timestamp']}\n\n"
                    text += f"*{mem['event_type']}*\n"
                    if mem.get('report'):
                        text += f"{mem['report']}\n"
                    else:
                        text += "Звіт порожній.\n"
                    
                    weights = mem.get('best_weights', {})
                    if weights:
                        text += "\n*Поточні ваги факторів:*\n"
                        for k, v in sorted(weights.items(), key=lambda x: x[1], reverse=True):
                            text += f"• {k}: {v*100:.1f}%\n"
                else:
                    text = "🤷‍♂️ AI Memory поки порожня. Еволюція ще не запускалася."

                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10
                )
            elif data == "menu_main_blacklist":
                answer_callback(token, callback["id"], "Завантажую бан-лист...")
                import db_logger
                bans = db_logger.get_active_blacklist()
                if bans:
                    text = f"🚫 *Активні бани ({len(bans)}):*\n━━━━━━━━━━━━━━━\n"
                    for b in bans[:15]:
                        sym = b.get('symbol', '?').replace('/USDT:USDT', '')
                        lvl = b.get('loss_count', 0)
                        exp = b.get('expires_at', '?')
                        text += f"• *{sym}* | Рівень: {lvl} | До: {exp}\n"
                else:
                    text = "✅ Бан-лист порожній. Всі монети дозволені."
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10
                )
            elif data == "menu_main_regime":
                answer_callback(token, callback["id"], "Перевіряю ринок...")
                try:
                    from regime_filter import check_market_regime
                    import ccxt
                    ex = ccxt.bybit({"options": {"defaultType": "swap"}})
                    regime = check_market_regime(ex)
                    r_emoji = {"TREND": "📈", "CHOP": "🔀", "VOLATILE": "⚡", "MANIPULATION": "🛑"}.get(regime['regime'], "❓")
                    bias = regime.get('direction_bias', 'NEUTRAL')
                    b_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(bias, "⚪")
                    text = (
                        f"🌡️ *Режим Ринку (BTC)*\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"{r_emoji} Режим: *{regime['regime']}*\n"
                        f"{b_emoji} Bias: *{bias}*\n"
                        f"📊 ADX: *{regime['adx']:.1f}*\n"
                        f"📈 EMA slope: *{regime['ema_slope']:.3f}%*\n"
                        f"⚡ ATR ratio: *{regime['atr_ratio']:.2f}*\n"
                        f"🕯 Wick ratio: *{regime['wick_ratio']:.0%}*\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"Торгівля: *{'✅ Дозволено' if regime['allow_trading'] else '❌ Заблоковано'}*"
                    )
                except Exception as e:
                    text = f"⚠️ Помилка: {e}"
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10
                )
            elif data.startswith("menu_"):
                _USER_STATES[chat_id] = data
                prompt_messages = {
                    "menu_risk_pct": "💰 Введіть новий ризик на угоду у відсотках (наприклад: `1.2`):",
                    "menu_max_concurrent_positions": "🛡 Введіть новий ліміт відкритих позицій (наприклад: `25`):",
                    "menu_max_active_orders": "🛡 Введіть новий ліміт активних ордерів (наприклад: `25`):",
                    "menu_tp1_rr": "🎯 Введіть TP1 R:R ціль (наприклад: `1.5`):",
                    "menu_tp2_rr": "🎯 Введіть TP2 R:R ціль (наприклад: `3.0`):",
                    "menu_auto_execute_confidence_threshold": "🧠 Введіть поріг автоматичного входу (наприклад, `0.65` для 65%):"
                }
                prompt = prompt_messages.get(data, "Введіть нове значення:")
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": prompt,
                        "parse_mode": "Markdown"
                    },
                    timeout=10
                )
                answer_callback(token, callback["id"], "Очікування введення...")

        # 2. Обробка текстових повідомлень (налаштування)
        msg = update.get("message")
        if msg:
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "").strip()
            if not text:
                continue

            if text in ("/menu", "/start"):
                _USER_STATES.pop(chat_id, None)
                send_main_menu(token, chat_id)
            elif text == "/settings":
                _USER_STATES.pop(chat_id, None)
                send_settings_menu(token, chat_id)
            elif chat_id in _USER_STATES:
                state = _USER_STATES.pop(chat_id)
                key_map = {
                    "menu_risk_pct": ("risk_pct", float),
                    "menu_max_concurrent_positions": ("max_concurrent_positions", int),
                    "menu_max_active_orders": ("max_active_orders", int),
                    "menu_tp1_rr": ("tp1_rr", float),
                    "menu_tp2_rr": ("tp2_rr", float),
                    "menu_auto_execute_confidence_threshold": ("auto_execute_confidence_threshold", float)
                }
                
                if state in key_map:
                    key, cast_func = key_map[state]
                    try:
                        val = cast_func(text)
                        # Додаткова валідація
                        if key == "auto_execute_confidence_threshold" and not (0.0 <= val <= 1.0):
                            raise ValueError("Поріг має бути між 0.0 та 1.0")
                        if key == "risk_pct" and not (0.0 < val <= 10.0):
                            raise ValueError("Ризик має бути в межах від 0.1% до 10%")
                            
                        if update_config_value(key, val):
                            resp_text = f"✅ Параметр *{key}* успішно змінено на *{val}*!"
                        else:
                            resp_text = "❌ Помилка оновлення конфігурації."
                    except Exception as e:
                        resp_text = f"❌ Невірний формат значення: {e}. Операцію скасовано."
                else:
                    resp_text = "❌ Невідомий стан редагування."

                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": resp_text,
                        "parse_mode": "Markdown"
                    },
                    timeout=10
                )
                # Повертаємо оновлене меню
                send_settings_menu(token, chat_id)


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
