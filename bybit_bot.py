import os
import time
import threading
import json
import ccxt
import numpy as np
import requests
import http.server
import socketserver
from ccxt.base.errors import RateLimitExceeded, BadSymbol
from json import JSONDecodeError
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from racer_core import analyze_racer
from telegram_notifier import (
    send_telegram_message,
    send_signal,
    send_signal_with_buttons,
    poll_telegram_callbacks,
    send_position_opened,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from db_logger import init_db, log_trade, update_trade_status, get_open_trades, get_trade_by_order_id, update_breakeven_status, is_blacklisted, blacklist_symbol, get_symbol_loss_count, cleanup_expired_blacklist, get_active_blacklist
from ai_signal_agent import generate_ai_signal
from quant_engine import score_setup, learn_from_trade
from adaptive_filters import AdaptiveFilterManager
from pnl_tracker import record_trade, get_summary
from ops_dashboard import record_cycle, record_event, build_24h_report
from regime_filter import check_market_regime
from logging_config import setup_file_logging

LOG_FILE = setup_file_logging("bot")

def start_health_server():
    """Фоновий HTTP-сервер для проходження health check на Render."""
    port = int(os.environ.get("PORT", 10000))
    
    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health"):
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.end_headers()
                
        def log_message(self, format, *args):
            pass
            
    def _run():
        try:
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("", port), HealthHandler) as httpd:
                print(f"[{datetime.now()}] 🏥 Health-сервер запущено на порту {port}")
                httpd.serve_forever()
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Не вдалося запустити Health-сервер: {e}")
            
    threading.Thread(target=_run, name="health-server", daemon=True).start()


load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# V11.2: Persistent Disk для конфігу
_data_dir = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_data_dir, 'active_config.json')

# V11.3 fix: Завжди оновлюємо /data/active_config.json з git-версії при старті
_script_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'active_config.json')
if CONFIG_PATH != _script_config and os.path.exists(_script_config):
    try:
        import shutil
        shutil.copy(_script_config, CONFIG_PATH)
        print(f"[startup] ✅ active_config.json оновлено з git → {CONFIG_PATH}")
    except Exception as e:
        print(f"[startup] ⚠️ Не вдалося оновити active_config.json до /data: {e}")

TIMEFRAME = "15m"
MIN_CANDLES_REQUIRED = 50
DEBUG_PAIRS = set()  # V10.2: Вимкнено для продакшн (було BEAT, BILL)

# V9.0: Module-level daily loss counter (thread-safe via GIL)
_daily_loss_counter = {"count": 0, "day": None}


def format_signal(signal: dict) -> str:
    def fmt_price(p: float) -> str:
        if p >= 1:
            return f"{p:.4f}"
        elif p >= 0.01:
            return f"{p:.5f}"
        elif p >= 0.001:
            return f"{p:.6f}"
        else:
            return f"{p:.8f}"

    direction = signal["direction"]
    symbol = signal["symbol"].replace("/USDT:USDT", "")
    emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    entry = float(signal["entry"])
    sl = float(signal["sl"])
    tp1 = float(signal["tp1"])
    tp2 = float(signal["tp2"])
    risk = abs(entry - sl)
    reward = abs(tp1 - entry)
    rr = round(reward / risk, 1) if risk > 0 else 0
    atr_val = float(signal.get("atr", 0) or 0)
    atr_str = f"{atr_val:.6f}" if atr_val < 0.001 else f"{atr_val:.4f}"
    return (
        f"⚡ <b>{emoji} | {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 Вхід:  <b>{fmt_price(entry)}</b>\n"
        f"🛡 SL:    <b>{fmt_price(sl)}</b>\n"
        f"🎯 TP1:  <b>{fmt_price(tp1)}</b>\n"
        f"🎯 TP2:  <b>{fmt_price(tp2)}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 R:R = 1:{rr} | ATR={atr_str}\n"
        f"🕐 {datetime.now().strftime('%H:%M %d.%m')}"
    )

# Зберігаємо стан для кожної пари (остання свічка, де знайдено сетап)
last_setup_bars = {}
last_order_times = {}
# FIXED: глобальні обмеження ризику депозиту.
MAX_DAILY_LOSS_PCT = 25.0
MAX_SESSION_DRAWDOWN_PCT = 20.0
MIN_ORDER_INTERVAL_SEC = 1.2

def safe_api_call(fn, *args, retries=3, base_sleep=1.0, **kwargs):
    # FIXED: retry/backoff wrapper для нестабільного Bybit API.
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except RateLimitExceeded:
            time.sleep(base_sleep * attempt)
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(base_sleep * attempt)
    return None

def calculate_kelly_risk(base_risk_pct: float, quant_score: float) -> float:
    """Розраховує ризик за спрощеним критерієм Келлі на основі квантової оцінки."""
    score = float(quant_score or 0.65)
    # Якщо сигнал дуже слабкий, ріжемо ризик навпіл
    if score < 0.5:
        return base_risk_pct * 0.5
    # Множник від 0.8 до 2.5 (експоненційний ріст при високій впевненості)
    multiplier = (score / 0.65) ** 2
    # Hard Cap: Келлі не може перевищувати базовий ризик більше ніж у 2.5 рази або 2.0% абсолютно
    kelly_risk = base_risk_pct * multiplier
    return min(max(kelly_risk, 0.1), 2.0)

def extract_usdt_equity(balance: dict, fallback: float = 10000.0) -> float:
    """Return account equity/total balance, not free margin.

    Risk guards must use equity/total. Using `free` falsely shows huge DD when
    margin is locked in open orders/positions.
    """
    if not isinstance(balance, dict):
        return fallback

    usdt = balance.get("USDT") or {}
    for key in ("total", "equity", "free"):
        try:
            value = float(usdt.get(key) or 0.0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    info = balance.get("info") or {}
    result = info.get("result") or {}
    accounts = result.get("list") or []
    for account in accounts:
        for key in ("totalEquity", "totalWalletBalance", "walletBalance"):
            try:
                value = float(account.get(key) or 0.0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        for coin in account.get("coin", []) or []:
            if str(coin.get("coin", "")).upper() == "USDT":
                for key in ("equity", "walletBalance", "usdValue"):
                    try:
                        value = float(coin.get(key) or 0.0)
                        if value > 0:
                            return value
                    except (TypeError, ValueError):
                        pass

    return fallback


def build_runtime_config(base_config: dict, dry_cycles_without_setups: int) -> dict:
    """Поступово послаблює фільтри, якщо ринок довго без сигналів."""
    cfg = dict(base_config)

    if dry_cycles_without_setups >= 3:
        cfg["vol_mult"] = max(0.85, float(cfg.get("vol_mult", 1.1)) - 0.1)
    if dry_cycles_without_setups >= 5:
        cfg["fvg_min_size"] = max(0.05, float(cfg.get("fvg_min_size", 0.2)) - 0.05)
    if dry_cycles_without_setups >= 7:
        cfg["adx_thresh"] = max(8, int(cfg.get("adx_thresh", 15)) - 2)
    if dry_cycles_without_setups >= 10:
        cfg["adx_min"] = max(10.0, float(cfg.get("adx_min", 12.0)) - 0.5)
        cfg["vol_multiplier_min"] = max(0.7, float(cfg.get("vol_multiplier_min", cfg.get("vol_mult", 1.0))) - 0.05)

    return cfg



def select_symbols_for_scan(exchange, symbols: list, config: dict, cycle_index: int) -> list:
    """Режим розвідки: пріоритезує топ-ліквідні пари та ротує хвіст списку."""
    max_symbols = int(config.get("max_symbols", 120))
    scout_top_n = int(config.get("scout_top_n", 40))
    rotate_step = int(config.get("rotate_step", 20))

    if not symbols:
        return []

    try:
        from async_scanner import get_tickers_parallel
        tickers = get_tickers_parallel(symbols)
        def quote_volume(sym):
            t = tickers.get(sym, {}) if isinstance(tickers, dict) else {}
            return float(t.get("quoteVolume") or t.get("baseVolume") or 0.0)
        ranked = sorted(symbols, key=quote_volume, reverse=True)
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Не вдалося ранжувати по ліквідності: {e}")
        ranked = list(symbols)

    head = ranked[:max(0, min(scout_top_n, max_symbols))]
    tail_pool = ranked[len(head):]

    if not tail_pool or len(head) >= max_symbols:
        return ranked[:max_symbols]

    rotation_offset = (cycle_index * max(1, rotate_step)) % len(tail_pool)
    rotated_tail = tail_pool[rotation_offset:] + tail_pool[:rotation_offset]
    tail_needed = max_symbols - len(head)

    return head + rotated_tail[:tail_needed]

def load_dynamic_config():
    """Динамічно завантажує налаштування стратегії з active_config.json."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                return json.load(f)
        except JSONDecodeError as e:
            print(f"Помилка завантаження active_config.json: {e}")
            # Fallback: інколи після ручного merge в файлі залишається зайвий хвіст.
            # Підхоплюємо перший валідний JSON-об'єкт, щоб бот не падав/не втрачав runtime-конфіг.
            try:
                with open(CONFIG_PATH, 'r') as f:
                    raw = f.read()
                decoder = json.JSONDecoder()
                parsed, idx = decoder.raw_decode(raw.lstrip())
                if isinstance(parsed, dict):
                    print(
                        f"[{datetime.now()}] ⚠️ active_config.json містить зайві дані після JSON (позиція {idx}). "
                        "Використовуємо перший валідний об'єкт."
                    )
                    # Самолікування: перезаписуємо файл чистим JSON, щоб прибрати помилку назавжди.
                    try:
                        temp_path = CONFIG_PATH + ".tmp"
                        with open(temp_path, 'w') as wf:
                            json.dump(parsed, wf, ensure_ascii=False, indent=4)
                            wf.write("\n")
                        os.replace(temp_path, CONFIG_PATH)
                        print(f"[{datetime.now()}] 🛠 active_config.json автоматично очищено від зайвих даних.")
                    except Exception as write_error:
                        print(f"[{datetime.now()}] ⚠️ Не вдалося авто-відновити active_config.json: {write_error}")
                    return parsed
            except Exception as fallback_error:
                print(f"[{datetime.now()}] ⚠️ Fallback-парсинг active_config.json не вдався: {fallback_error}")
        except Exception as e:
            print(f"Помилка завантаження active_config.json: {e}")
    
    # Дефолтні налаштування
    return {
        "use_demo": True,
        "base_url": "https://api-demo.bybit.com",
        "api_key": "",
        "api_secret": "",
        "dry_run": True,
        "fib_level": 0.5,
        "sl_atr_mult": 1.0,
        "tp1_rr": 1.0,
        "tp2_rr": 2.5,
        "risk_pct": 0.5,
        "leverage": 3.0,
        "max_position_notional_pct": 30.0,
        "liq_lookback": 20,
        "adx_thresh": 15,
        "adx_min": 12,
        "adx_adaptive_window": 20,
        "adx_adaptive_factor": 0.7,
        "vol_mult": 1.0,
        "vol_multiplier_min": 0.8,
        "fvg_min_size": 0.08,
        "max_symbols": 120,
        "symbol_offset": 0,
        "scout_top_n": 40,
        "rotate_step": 20,
    }

def get_all_usdt_symbols(exchange, max_symbols=None):
    """Отримує всі активні USDT ф'ючерси на Bybit."""
    print(f"[{datetime.now()}] Завантаження списку монет з Bybit...")
    exchange.load_markets()
    symbols = []
    for symbol, market in exchange.markets.items():
        if market.get('linear') and market.get('quote') == 'USDT' and market.get('type') == 'swap':
            if market.get('active'):
                symbols.append(symbol)
    
    symbols = sorted(symbols)

    if max_symbols:
        print(f"[{datetime.now()}] Обмежуємо сканування до {max_symbols} пар (щоб не впиратись у Rate Limit).")
        symbols = symbols[:max_symbols]

    print(f"[{datetime.now()}] Знайдено {len(symbols)} USDT ф'ючерсних пар для сканування!")
    return symbols

def init_bybit(config: dict):
    """Ініціалізація Bybit для демо-торгівлі за допомогою API-ключів."""
    use_demo = bool(config.get("use_demo", True))
    base_url = config.get("base_url") or ("https://api-demo.bybit.com" if use_demo else "https://api.bybit.com")
    api_key = config.get("api_key") or API_KEY
    api_secret = config.get("api_secret") or API_SECRET

    if use_demo:
        print(f"[{datetime.now()}] ⚠️ DEMO MODE — реальні гроші не використовуються ({base_url})")
    else:
        print(f"[{datetime.now()}] 🔴 LIVE MODE — реальні гроші! ({base_url})")

    exchange_params = {
        'enableRateLimit': True,
        'timeout': 15000,
        'urls': {'api': base_url},
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
            'recvWindow': 10000
        }
    }
    
    if api_key and api_secret:
        exchange_params['apiKey'] = api_key
        exchange_params['secret'] = api_secret
        print(f"[{datetime.now()}] API-ключі виявлені. Авторизуємось на Bybit.")
    else:
        print(f"[{datetime.now()}] API-ключі відсутні. Використовуємо публічний доступ.")

    exchange = ccxt.bybit(exchange_params)
    
    # Активуємо режим Demo Trading для безпеки
    try:
        if use_demo:
            exchange.enableDemoTrading(True)
            print(f"[{datetime.now()}] 🟢 Успішно активовано BYBIT DEMO TRADING.")
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Не вдалося активувати Demo Trading: {e}")

    try:
        exchange.load_time_difference()
    except Exception as e:
        print(f"Попередження при синхронізації часу: {e}")
        
    return exchange

def fetch_data(exchange, symbol, timeframe, limit=100):
    """Отримує OHLCV дані з біржі."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    if len(df) < MIN_CANDLES_REQUIRED:
        print(f"[{datetime.now()}] ⚠️ Пропускаємо {symbol} {timeframe}: мало свічок ({len(df)})")
        return None
    return df

def validate_trade_levels(direction: str, entry: float, sl: float, tp1: float, tp2: float) -> tuple[bool, str]:
    """Перевіряє, що SL/TP стоять з правильного боку від entry для Bybit TP/SL."""
    entry = float(entry)
    sl = float(sl)
    tp1 = float(tp1)
    tp2 = float(tp2)
    if direction == "LONG":
        if not (sl < entry < tp1 <= tp2):
            return False, f"LONG levels invalid: SL({sl}) < entry({entry}) < TP1({tp1}) <= TP2({tp2}) не виконується"
    elif direction == "SHORT":
        if not (tp2 <= tp1 < entry < sl):
            return False, f"SHORT levels invalid: TP2({tp2}) <= TP1({tp1}) < entry({entry}) < SL({sl}) не виконується"
    else:
        return False, f"unknown direction={direction}"
    return True, "ok"


def execute_demo_order(exchange, symbol, direction, entry, sl, tp1, tp2, risk_pct, leverage=3.0, max_position_notional_pct=30.0):
    """Виставляє реальний лімітний ордер на Bybit Demo з Stop Loss та Take Profit."""
    try:
        levels_ok, levels_reason = validate_trade_levels(direction, entry, sl, tp1, tp2)
        if not levels_ok:
            print(f"[{datetime.now()}] ⛔ Пропускаємо {symbol}: {levels_reason}")
            send_telegram_message(f"⛔ <b>{symbol}</b>: некоректні SL/TP — ордер не створено.<br><code>{levels_reason}</code>")
            return None

        # 0. Перевірка Order Book Imbalance (HFT Level 2 Filter)
        try:
            ob = safe_api_call(exchange.fetch_order_book, symbol, limit=50)
            if ob:
                bids = ob.get('bids', [])
                asks = ob.get('asks', [])
                current_price = bids[0][0] if bids else entry
                
                # Рахуємо об'єм в межах 1% від поточної ціни
                bid_vol = sum(amount for price, amount in bids if price >= current_price * 0.99)
                ask_vol = sum(amount for price, amount in asks if price <= current_price * 1.01)
                
                if direction == "LONG" and bid_vol > 0 and (ask_vol / bid_vol) >= 3.0:
                    cancel_reason = f"Order Book Imbalance: Ask стіна в {ask_vol/bid_vol:.1f}x більша за Bids"
                    print(f"[{datetime.now()}] 🛡 Скасовано {symbol}: {cancel_reason}")
                    send_telegram_message(f"🛡 <b>{symbol}</b>: LONG скасовано.<br><code>{cancel_reason}</code>")
                    return None
                elif direction == "SHORT" and ask_vol > 0 and (bid_vol / ask_vol) >= 3.0:
                    cancel_reason = f"Order Book Imbalance: Bid стіна в {bid_vol/ask_vol:.1f}x більша за Asks"
                    print(f"[{datetime.now()}] 🛡 Скасовано {symbol}: {cancel_reason}")
                    send_telegram_message(f"🛡 <b>{symbol}</b>: SHORT скасовано.<br><code>{cancel_reason}</code>")
                    return None
        except Exception as e_ob:
            print(f"[{datetime.now()}] ⚠️ Помилка отримання стакану для {symbol}: {e_ob}")

        # 1. Отримуємо демо-баланс
        balance_info = safe_api_call(exchange.fetch_balance)
        free_usdt = balance_info.get('USDT', {}).get('free', 0.0)
        
        # Якщо баланс на демо рахунку нульовий або не отриманий, використовуємо умовний ліміт для тестів
        if free_usdt <= 0:
            free_usdt = 10000.0 # Дефолтний умовний демо-баланс

        # 2. Розраховуємо ризик і об'єм
        price_diff = abs(entry - sl)
        if price_diff <= 0:
            return None
            
        # V11.3: Risk cap збільшено до 2.0% (було 1.0%) — конфіг керує через risk_pct
        risk_pct = min(max(float(risk_pct), 0.1), 2.0)
        risk_amount = free_usdt * (risk_pct / 100.0)
        qty = risk_amount / price_diff
        leverage = min(max(float(leverage), 1.0), 10.0)
        max_position_notional_pct = min(max(float(max_position_notional_pct), 1.0), 100.0)
        max_notional = free_usdt * (max_position_notional_pct / 100.0) * leverage
        if entry > 0 and qty * entry > max_notional:
            qty = max_notional / entry
            print(f"[{datetime.now()}] 🧯 Qty capped by notional limit: {symbol} max_notional={max_notional:.2f} USDT")
        
        # Отримуємо специфікації монети та форматуємо ціну/кількість до точності біржі
        price_str = exchange.price_to_precision(symbol, entry)
        market = exchange.market(symbol)
        qty_str = exchange.amount_to_precision(symbol, qty)
        
        qty_val = float(qty_str)
        if qty_val <= 0:
            # Захист від занадто малих позицій на дешевих монетах
            qty_str = exchange.amount_to_precision(symbol, qty * 10)
            qty_val = float(qty_str)

        # FIXED: лімітуємо кількість контрактів згідно біржових min/max, щоб не ловити retCode 10001.
        amount_limits = (market.get("limits", {}) or {}).get("amount", {}) or {}
        min_qty = amount_limits.get("min")
        max_qty = amount_limits.get("max")
        if max_qty is not None and qty_val > float(max_qty):
            qty_val = float(max_qty)
            qty_str = exchange.amount_to_precision(symbol, qty_val)
        if min_qty is not None and qty_val < float(min_qty):
            qty_val = float(min_qty)
            qty_str = exchange.amount_to_precision(symbol, qty_val)
        if float(qty_str) <= 0:
            return None
            
        side = 'buy' if direction == "LONG" else 'sell'
        
        # Налаштовуємо параметри TP/SL для Bybit V5
        params = {
            'takeProfit': exchange.price_to_precision(symbol, tp2),
            'stopLoss': exchange.price_to_precision(symbol, sl),
            'tpslMode': 'Full'
        }
        
        # Встановлюємо контрольоване плече перед угодою
        try:
            exchange.set_leverage(leverage, symbol)
        except Exception:
            pass # Плече вже встановлене
            
        print(f"[{datetime.now()}] 📤 Відправляємо ордер на DEMO: {symbol} {direction}")
        print(f"[{datetime.now()}] 🛒 ДЕМО-ОРДЕР: {side.upper()} {qty_str} {symbol} по {price_str} (SL: {sl:.4f}, TP: {tp2:.4f})")
        
        now_ts = time.time()
        # FIXED: анти-дубль ордера по одному символу.
        if (now_ts - last_order_times.get(symbol, 0.0)) < MIN_ORDER_INTERVAL_SEC:
            return None
        # V11.3: Aggressive Limit Order (0.1% offset від entry) замість Market.
        # Це зменшує slippage та market impact на малоліквідних монетах.
        offset_pct = 0.001  # 0.1%
        if direction == "LONG":
            limit_price = entry * (1.0 + offset_pct)  # Трохи вище — дозволяємо заповнення
        else:
            limit_price = entry * (1.0 - offset_pct)  # Трохи нижче — для SHORT
        limit_price_str = exchange.price_to_precision(symbol, limit_price)
        order = safe_api_call(
            exchange.create_order,
            symbol=symbol,
            type='limit',
            side=side,
            amount=float(qty_str),
            price=float(limit_price_str),
            params=params
        )
        if not order:
            return None
        if isinstance(order, dict):
            order["_bot_amount"] = qty_str
            order["_bot_leverage"] = leverage
            order["_bot_max_notional"] = max_notional
        last_order_times[symbol] = now_ts
        
        send_telegram_message(
            f"🛒 <b>ОРДЕР ВИСТАВЛЕНО НА DEMO (Limit+offset)</b>\n"
            f"Монета: <b>{symbol}</b> | Напрямок: <b>{direction}</b>\n"
            f"Ліміт: <b>{limit_price_str}</b> (entry={price_str})\n"
            f"Об'єм: <b>{qty_str}</b>\n"
            f"Stop Loss: <b>{sl:.4f}</b> | Take Profit 2: <b>{tp2:.4f}</b>\n"
            f"ID ордера: <code>{order.get('id', 'N/A')}</code>"
        )
        return order
    except Exception as e:
        print(f"⚠️ Помилка виставлення демо-ордера на {symbol}: {e}")
        send_telegram_message(f"❌ <b>Помилка ордера {symbol}:</b> {e}")
        return None


def get_open_positions(exchange):
    positions = safe_api_call(exchange.fetch_positions) or []
    return [p for p in positions if float(p.get("contracts") or p.get("positionAmt") or p.get("info", {}).get("size") or 0) != 0]

def get_open_orders(exchange, symbol=None):
    """Повертає відкриті ордери (опційно по символу)."""
    try:
        if symbol:
            return safe_api_call(exchange.fetch_open_orders, symbol=symbol) or []
        return safe_api_call(exchange.fetch_open_orders) or []
    except Exception:
        return []

def fetch_closed_pnl_bybit(exchange, symbol):
    """Отримує історію закритих PnL для символу з Bybit V5 API."""
    try:
        market = exchange.market(symbol)
        market_id = market['id']
        params = {
            'category': 'linear',
            'symbol': market_id,
            'limit': 20
        }
        response = safe_api_call(exchange.private_get_v5_position_closed_pnl, params)
        if response and isinstance(response, dict) and response.get('retCode') == 0:
            records = response.get('result', {}).get('list', [])
            if isinstance(records, list):
                records.sort(key=lambda x: int(x.get("createdTime") or 0))
                return records
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Помилка fetch_closed_pnl_bybit для {symbol}: {e}")
    return []

def has_same_direction_open_order(orders, direction: str) -> bool:
    side_target = "buy" if direction == "LONG" else "sell"
    for o in orders:
        side = str(o.get("side") or o.get("info", {}).get("side", "")).lower()
        if side == side_target:
            return True
    return False


def can_open_position(symbol: str, direction: str, open_positions: list, open_orders: list, config: dict, notify_tg: bool = False) -> bool:
    # 1. Check if any position (LONG or SHORT) is already open for this symbol to prevent opposite direction orders and Bybit SL errors in One-Way Mode
    symbol_positions = [p for p in open_positions if p.get("symbol") == symbol]
    if symbol_positions:
        p = symbol_positions[0]
        side = str(p.get("side") or p.get("info", {}).get("side", "")).upper()
        msg = f"⛔ Позиція {side} вже відкрита для {symbol} — пропускаємо новий {direction} ордер"
        print(f"[{datetime.now()}] {msg}")
        if notify_tg:
            send_telegram_message(msg)
        return False
            
    # 2. Check global portfolio max concurrent positions and active limit orders limits separately
    max_positions = int(config.get("max_concurrent_positions", 15))
    max_orders = int(config.get("max_active_orders", 15))
    
    if len(open_positions) >= max_positions:
        msg = f"⛔ Досягнуто ліміт відкритих позицій портфеля ({len(open_positions)}/{max_positions}) — пропускаємо {symbol}"
        print(f"[{datetime.now()}] {msg}")
        if notify_tg:
            send_telegram_message(msg)
        return False
        
    if len(open_orders) >= max_orders:
        msg = f"⛔ Досягнуто ліміт активних ордерів портфеля ({len(open_orders)}/{max_orders}) — пропускаємо {symbol}"
        print(f"[{datetime.now()}] {msg}")
        if notify_tg:
            send_telegram_message(msg)
        return False
        
    return True


def safe_cancel_order(exchange, order_id, symbol):
    """Безпечно скасовує ордер на Bybit, не допускаючи крашу при OrderNotFound."""
    try:
        return safe_api_call(exchange.cancel_order, order_id, symbol)
    except ccxt.OrderNotFound:
        print(f"[{datetime.now()}] ℹ️ Ордер {order_id} ({symbol}) вже не існує на Bybit (можливо, заповнений або скасований).")
        return None
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Не вдалося скасувати ордер {order_id} ({symbol}): {e}")
        return None


def cancel_stale_orders(exchange, config: dict):
    """Скасовує лімітні ордери, що висять занадто довго або пробили SL до входу."""
    open_orders = get_open_orders(exchange)
    if not open_orders:
        return
        
    max_age_sec = int(config.get("max_order_age_seconds", 7200))
    now_ts = time.time()
    
    # Отримуємо тикери для перевірки ціни
    symbols_to_check = list(set(o.get("symbol") for o in open_orders if o.get("symbol")))
    tickers = {}
    if symbols_to_check:
        try:
            tickers = exchange.fetch_tickers(symbols_to_check)
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Помилка отримання тикерів для перевірки SL: {e}")
            
    for o in open_orders:
        symbol = o.get("symbol")
        order_id = o.get("id")
        timestamp = o.get("timestamp")  # milliseconds
        
        if not symbol or not order_id:
            continue
            
        # 1. Перевірка за часом життя
        if timestamp:
            age_sec = now_ts - (timestamp / 1000.0)
            if age_sec > max_age_sec:
                print(f"[{datetime.now()}] 🕒 Ордер {order_id} ({symbol}) застарів ({age_sec:.0f}s > {max_age_sec}s). Скасовуємо.")
                safe_cancel_order(exchange, order_id, symbol)
                send_telegram_message(f"🕒 <b>Скасовано застарілий ордер</b>\nМонета: <b>{symbol}</b>\nВік: {age_sec/60:.1f} хв")
                update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)
                continue
                
        # 2. Перевірка пробиття Stop Loss до входу
        trade = get_trade_by_order_id(order_id)
        if trade and tickers.get(symbol):
            current_price = float(tickers[symbol].get("last") or tickers[symbol].get("close") or 0.0)
            sl = float(trade.get("stop_loss") or 0.0)
            direction = str(trade.get("direction")).upper()
            
            if current_price > 0 and sl > 0:
                if direction == "LONG" and current_price <= sl:
                    print(f"[{datetime.now()}] 🛑 Ціна {current_price} пробила SL {sl} для LONG {symbol} до входу. Скасовуємо ордер {order_id}.")
                    safe_cancel_order(exchange, order_id, symbol)
                    send_telegram_message(f"🛑 <b>Скасовано ордер (SL пробито до входу)</b>\nМонета: <b>{symbol}</b>\nЦіна: {current_price} | SL: {sl}")
                    update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)
                elif direction == "SHORT" and current_price >= sl:
                    print(f"[{datetime.now()}] 🛑 Ціна {current_price} пробила SL {sl} для SHORT {symbol} до входу. Скасовуємо ордер {order_id}.")
                    safe_cancel_order(exchange, order_id, symbol)
                    send_telegram_message(f"🛑 <b>Скасовано ордер (SL пробито до входу)</b>\nМонета: <b>{symbol}</b>\nЦіна: {current_price} | SL: {sl}")
                    update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)


def set_bybit_position_sl(exchange, symbol, direction, new_sl):
    """Встановлює новий Stop Loss для позиції на Bybit V5."""
    try:
        market = exchange.market(symbol)
        params = {
            'category': 'linear',
            'symbol': market['id'],
            'stopLoss': exchange.price_to_precision(symbol, new_sl),
            'tpslMode': 'Full',
            'positionIdx': 0
        }
        return safe_api_call(exchange.privateLinearPostPositionTradingStop, params)
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Не вдалося встановити SL для {symbol} на Bybit: {e}")
        return None

def manage_active_positions(exchange, positions: list, config: dict) -> list:
    """
    V11.2: Активний менеджер позицій на Bybit.
    Перевіряє:
    1) Збиток <= -$300 (або max_position_loss_usd)
    2) Прибуток >= +20% від об'єму (notional size)
    3) Таймаут 4 години (14400 секунд)
    Повертає список символів, які були закриті.
    """
    closed_symbols = []
    if not positions:
        return closed_symbols

    for pos in positions:
        try:
            symbol = pos.get("symbol")
            if not symbol:
                continue
            
            contracts = float(pos.get("contracts") or pos.get("info", {}).get("size") or 0.0)
            entry_price = float(pos.get("entryPrice") or pos.get("info", {}).get("entryPrice") or 0.0)
            unrealized_pnl = float(pos.get("unrealizedPnl") or pos.get("info", {}).get("unrealisedPnl") or 0.0)
            side = str(pos.get("side") or pos.get("info", {}).get("side", "")).lower()
            
            if contracts <= 0 or entry_price <= 0:
                continue
                
            position_notional = contracts * entry_price
            
            # Параметри з конфігу (з дефолтами)
            max_loss_usd = float(config.get("max_position_loss_usd", 300.0))
            take_profit_pct = float(config.get("take_profit_pct_of_size", 20.0))
            max_age_seconds = float(config.get("max_position_age_seconds", 14400.0))
            
            should_close = False
            close_reason = ""
            
            # 1. Перевіряємо жорсткий стоп по сумі
            if unrealized_pnl <= -max_loss_usd:
                should_close = True
                close_reason = f"збиток {unrealized_pnl:.2f} USDT <= -{max_loss_usd} USDT"
                
            # 2. Перевіряємо фіксацію прибутку (+20%)
            elif position_notional > 0 and (unrealized_pnl / position_notional) * 100.0 >= take_profit_pct:
                should_close = True
                close_reason = f"прибуток {unrealized_pnl:.2f} USDT >= {take_profit_pct}% від об'єму ({position_notional:.2f} USDT)"
                
            # 3. Перевіряємо таймаут (4 години)
            else:
                age_seconds = None
                # Спочатку шукаємо відкриту угоду в нашій БД
                try:
                    from db_logger import get_db_conn
                    with get_db_conn() as conn:
                        row = conn.execute(
                            "SELECT timestamp FROM trades WHERE symbol = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
                            (symbol,)
                        ).fetchone()
                        if row:
                            trade_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                            age_seconds = (datetime.now() - trade_time).total_seconds()
                except Exception as e_db:
                    print(f"[{datetime.now()}] ⚠️ Не вдалося прочитати час з БД для {symbol}: {e_db}")
                
                # Якщо немає в БД (сирота) — запитуємо історію Bybit
                if age_seconds is None:
                    try:
                        my_trades = safe_api_call(exchange.fetch_my_trades, symbol, limit=1)
                        if my_trades:
                            trade_ts = my_trades[0].get("timestamp")
                            if trade_ts:
                                age_seconds = (time.time() - (trade_ts / 1000.0))
                    except Exception as e_trades:
                        print(f"[{datetime.now()}] ⚠️ Не вдалося завантажити історію з Bybit для {symbol}: {e_trades}")
                
                if age_seconds is not None and age_seconds >= max_age_seconds:
                    should_close = True
                    close_reason = f"таймаут {age_seconds/3600:.1f} год >= {max_age_seconds/3600:.1f} год (PnL: {unrealized_pnl:.2f} USDT)"
            
            if should_close:
                close_side = "sell" if side in {"buy", "long"} else "buy"
                print(f"[{datetime.now()}] 🚨 Smart Monitor: закриваємо позицію {symbol} ({side}) по ринку — {close_reason}")
                
                order = safe_api_call(
                    exchange.create_order,
                    symbol=symbol,
                    type="market",
                    side=close_side,
                    amount=contracts,
                    price=None
                )
                
                if order:
                    status_outcome = "WIN" if unrealized_pnl >= 0 else "LOSS"
                    update_trade_status(symbol=symbol, status=status_outcome, pnl=unrealized_pnl)
                    record_trade(symbol, "LONG" if side in {"buy", "long"} else "SHORT", entry_price, entry_price * (1.0 + unrealized_pnl/position_notional if position_notional else 1.0), unrealized_pnl)
                    
                    send_telegram_message(
                        f"🚨 <b>Smart Monitor: Позицію Закрито</b>\n"
                        f"Монета: <b>{symbol}</b> ({side.upper()})\n"
                        f"Причина: <code>{close_reason}</code>\n"
                        f"PnL: <b>{unrealized_pnl:.2f} USDT</b>"
                    )
                    closed_symbols.append(symbol)
        except Exception as e_pos:
            print(f"[{datetime.now()}] ⚠️ Помилка обробки активної позиції {pos.get('symbol')}: {e_pos}")
            
    return closed_symbols


import concurrent.futures

def sync_open_trades(exchange, config: dict):
    """Синхронізує відкриті угоди в базі даних з їх реальним статусом на Bybit."""
    try:
        # Отримуємо відкриті позиції на біржі
        positions = get_open_positions(exchange)
        
        # V11.2: Активний менеджер позицій (стоп по сумі, тейк по %, таймаут, сироти)
        closed_symbols = manage_active_positions(exchange, positions, config)
        if closed_symbols:
            positions = get_open_positions(exchange)
            
        open_trades = get_open_trades()
        if not open_trades:
            return
        
        # Отримуємо вільний баланс один раз для розрахунку віртуальних PnL
        free_usdt = 50000.0
        try:
            balance_info = safe_api_call(exchange.fetch_balance) or {}
            free_usdt = float(balance_info.get('USDT', {}).get('free', 50000.0))
            if free_usdt <= 0:
                free_usdt = 50000.0
        except Exception:
            pass
            
        def process_trade(t):
            symbol = t.get("symbol")
            direction = t.get("direction")
            order_id = t.get("order_id")
            entry_price = t.get("entry_price")
            # V11: entry_price може бути bytes через numpy.float32 баг
            if entry_price is not None:
                try:
                    entry_price = float(entry_price)
                except (TypeError, ValueError):
                    if isinstance(entry_price, bytes):
                        import struct
                        try: entry_price = struct.unpack('f', entry_price)[0]
                        except: entry_price = 0.0
                    else:
                        entry_price = 0.0
            
            if not symbol:
                return
                
            status_current = t.get("status")
            is_virtual = bool(status_current == "VIRTUAL_OPEN")

            # 0.1. Перевіряємо таймаут для віртуальних угод (48 годин)
            if is_virtual:
                try:
                    trade_time_str = t.get("timestamp")
                    trade_time_dt = datetime.strptime(trade_time_str, "%Y-%m-%d %H:%M:%S")
                    age_seconds = (datetime.now() - trade_time_dt).total_seconds()
                    if age_seconds >= 28800:  # V10.1: 8 годин (замість 48)
                        print(f"[{datetime.now()}] 🧠 Віртуальна угода {symbol} застаріла ({age_seconds/3600:.1f} год). Закриваємо за таймаутом.")
                        update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)
                        send_telegram_message(
                            f"🧠 <b>Virtual Trade Timeout</b>\n"
                            f"Монета: <b>{symbol}</b> ({direction})\n"
                            f"Віртуальну угоду скасовано після 48 годин очікування."
                        )
                        return
                except Exception as e_timeout:
                    print(f"[{datetime.now()}] ⚠️ Помилка перевірки таймауту для {symbol}: {e_timeout}")

            # 0. Перевіряємо та переводимо реальну позицію в безубиток при досягненні TP1
            if not is_virtual and t.get("breakeven_activated", 0) == 0:
                symbol_positions = [p for p in positions if p.get("symbol") == symbol]
                side_target = "buy" if direction == "LONG" else "sell"
                matching_pos = None
                for pos in symbol_positions:
                    side = str(pos.get("side") or pos.get("info", {}).get("side", "")).lower()
                    if side in {"buy", "long"} and side_target == "buy":
                        matching_pos = pos
                    elif side in {"sell", "short"} and side_target == "sell":
                        matching_pos = pos
                        
                if matching_pos:
                    current_price = float(matching_pos.get("markPrice") or matching_pos.get("info", {}).get("markPrice") or 0.0)
                    tp1 = t.get("take_profit_1") or (entry_price * 1.02 if direction == "LONG" else entry_price * 0.98)
                    if current_price > 0 and tp1 > 0:
                        activated = False
                        if direction == "LONG" and current_price >= tp1:
                            activated = True
                        elif direction == "SHORT" and current_price <= tp1:
                            activated = True
                            
                        if activated:
                            print(f"[{datetime.now()}] 🎉 TP1 досягнуто для реальної позиції {symbol} ({current_price}). Переводимо в безубиток та закриваємо 50% об'єму.")
                            contracts = float(matching_pos.get("contracts") or matching_pos.get("info", {}).get("size") or 0.0)
                            if contracts > 0:
                                qty_to_close = contracts * 0.5
                                qty_str = exchange.amount_to_precision(symbol, qty_to_close)
                                qty_val = float(qty_str)
                                if qty_val > 0:
                                    close_side = "sell" if direction == "LONG" else "buy"
                                    try:
                                        print(f"[{datetime.now()}] 📤 Закриваємо 50% ({qty_val}) позиції {symbol} ринковим ордером.")
                                        safe_api_call(
                                            exchange.create_order,
                                            symbol=symbol,
                                            type='market',
                                            side=close_side,
                                            amount=qty_val
                                        )
                                    except Exception as e_close:
                                        print(f"[{datetime.now()}] ⚠️ Не вдалося закрити 50% позиції {symbol}: {e_close}")
                                        
                            set_bybit_position_sl(exchange, symbol, direction, entry_price)
                            update_breakeven_status(symbol=symbol, order_id=order_id, status=1)
                            send_telegram_message(
                                f"🎉 <b>TP1 Досягнуто! (Безубиток)</b>\n"
                                f"Монета: <b>{symbol}</b> ({direction})\n"
                                f"Ціна: {current_price} | TP1: {tp1}\n"
                                f"🛡 50% об'єму закрито за ринком, SL для решти перенесено в безубиток ({entry_price})."
                            )

            # 1. Перевіряємо, чи є активна позиція по цьому символу
            symbol_positions = [p for p in positions if p.get("symbol") == symbol]
            side_target = "buy" if direction == "LONG" else "sell"
            has_active_position = False
            
            if not is_virtual:
                for p in symbol_positions:
                     side = str(p.get("side") or p.get("info", {}).get("side", "")).lower()
                     if side in {"buy", "long"} and side_target == "buy":
                         has_active_position = True
                     elif side in {"sell", "short"} and side_target == "sell":
                         has_active_position = True
                         
                if has_active_position:
                    # Позиція ще відкрита
                    return
                
            # 2. Якщо позиції немає, перевіряємо чи активний ще лімітний ордер на Bybit
            open_orders = get_open_orders(exchange, symbol)
            order_is_active = False
            if not is_virtual:
                if order_id:
                    order_is_active = any(o.get("id") == order_id for o in open_orders)
                else:
                    order_is_active = has_same_direction_open_order(open_orders, direction)
                    
                if order_is_active:
                    # Ордер ще чекає у стакані
                    return
                
            # 3. Угоду закрили або скасували
            was_filled = False
            actual_pnl = 0.0
            exit_price = entry_price
            
            if is_virtual:
                was_filled = True
            elif order_id:
                try:
                    order_info = exchange.fetch_order(order_id, symbol)
                    status = order_info.get("status")
                    if status == "canceled" or status == "rejected":
                        print(f"[{datetime.now()}] ℹ️ Ордер {order_id} ({symbol}) скасовано.")
                        update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)
                        return
                    elif status == "closed":
                        was_filled = True
                except Exception as e:
                    print(f"[{datetime.now()}] ⚠️ Не вдалося отримати статус ордера {order_id}: {e}")
                    # Вважаємо закритим, якщо його немає в активних
                    was_filled = True
            else:
                was_filled = True
                
            if was_filled:
                if not is_virtual:
                    try:
                        closed_pnl_records = fetch_closed_pnl_bybit(exchange, symbol)
                        if closed_pnl_records:
                            latest_pnl = closed_pnl_records[-1]
                            actual_pnl = float(latest_pnl.get("closedPnl") or 0.0)
                            exit_price = float(latest_pnl.get("avgExitPrice") or entry_price)
                            status_outcome = "WIN" if actual_pnl > 0 else "LOSS"
                            
                            # Самонавчання для Квантового Ядра
                            factors_str = t.get("factors_snapshot")
                            if factors_str:
                                try:
                                    factors_snap = json.loads(factors_str)
                                    if factors_snap and status_outcome in ("WIN", "LOSS"):
                                        learn_from_trade(factors_snap, status_outcome, actual_pnl)
                                        # V11 Phase 8: Record for active features
                                        import feature_manager
                                        is_win = (status_outcome == "WIN")
                                        for fname in feature_manager.manager.features.keys():
                                            if feature_manager.manager.is_enabled(fname):
                                                feature_manager.manager.record_result(fname, is_win)
                                except Exception as e_learn:
                                    print(f"[{datetime.now()}] ⚠️ Не вдалося запустити learn_from_trade: {e_learn}")

                            update_trade_status(symbol=symbol, status=status_outcome, pnl=actual_pnl, order_id=order_id)
                            print(f"[{datetime.now()}] 🎯 Угода {symbol} закрита: {status_outcome} PnL={actual_pnl:.4f} USDT")
                            # V9.1: Progressive auto-blacklist after loss
                            if actual_pnl < 0:
                                _daily_loss_counter["count"] += 1
                                _loss_count = get_symbol_loss_count(symbol, hours=24)
                                if _loss_count >= 3:
                                    ban_info = blacklist_symbol(symbol, f"{_loss_count} збитків за 24г")
                                    send_telegram_message(
                                        f"🚫 <b>{symbol}</b> заблоковано на {ban_info['hours']}г "
                                        f"(рівень {ban_info['level']}): {_loss_count} збитків"
                                    )
                            return
                    except Exception as e:
                        print(f"[{datetime.now()}] ⚠️ Помилка fetch_closed_pnl_bybit для {symbol}: {e}")
                    
                # Fallback за логікою цінових рівнів
                try:
                    raw_ohlcv = safe_api_call(exchange.fetch_ohlcv, symbol, "1m", limit=300)
                    if raw_ohlcv:
                        ohlcv = pd.DataFrame(raw_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                        
                        # FIXED: Виправлення Lookback Window Bug
                        try:
                            trade_time_str = t.get("timestamp")
                            trade_time_dt = datetime.strptime(trade_time_str, "%Y-%m-%d %H:%M:%S")
                            age_seconds = (datetime.now() - trade_time_dt).total_seconds()
                            trade_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - int(age_seconds * 1000)
                            # Залишаємо свічки, що почалися після відкриття угоди з буфером 1хв
                            ohlcv = ohlcv[ohlcv["timestamp"] >= (trade_time_ms - 60000)]
                        except Exception as e_time:
                            print(f"[{datetime.now()}] ⚠️ Не вдалося відфільтрувати свічки по часу для {symbol}: {e_time}")
                            
                        if ohlcv.empty:
                            # Немає свічок, які покривають період після відкриття угоди, чекаємо наступного циклу
                            return
                            
                        # V11: safe float conversion (existing records may have bytes from numpy.float32 bug)
                        def _safe_float(val, default):
                            if val is None: return float(default)
                            try: return float(val)
                            except (TypeError, ValueError):
                                if isinstance(val, bytes):
                                    import struct
                                    try: return struct.unpack('f', val)[0]
                                    except: return float(default)
                                return float(default)
                        
                        tp1 = _safe_float(t.get("take_profit_1"), entry_price * 1.02 if direction == "LONG" else entry_price * 0.98)
                        tp2 = _safe_float(t.get("take_profit_2"), entry_price * 1.05 if direction == "LONG" else entry_price * 0.95)
                        
                        # Поточний статус безубитку та Stop Loss
                        breakeven_activated = bool(t.get("breakeven_activated", 0) == 1)
                        sl_initial = _safe_float(t.get("stop_loss"), entry_price * 0.98 if direction == "LONG" else entry_price * 1.02)
                        sl_current = float(entry_price) if breakeven_activated else sl_initial
                        
                        reached_tp1 = False
                        reached_tp2 = False
                        reached_sl = False
                        
                        # Хронологічний перебір свічок (усунення Lookahead Bias)
                        for _, row in ohlcv.iterrows():
                            h_val = float(row["high"])
                            l_val = float(row["low"])
                            
                            if direction == "LONG":
                                # 1. Спочатку Stop Loss
                                if l_val <= sl_current:
                                    reached_sl = True
                                    break
                                # 2. Потім TP1 (безубиток)
                                if not breakeven_activated and not reached_tp1 and h_val >= tp1:
                                    reached_tp1 = True
                                    breakeven_activated = True
                                    sl_current = entry_price
                                # 3. Потім TP2
                                if h_val >= tp2:
                                    reached_tp2 = True
                                    break
                            else: # SHORT
                                # 1. Спочатку Stop Loss
                                if h_val >= sl_current:
                                    reached_sl = True
                                    break
                                # 2. Потім TP1 (безубиток)
                                if not breakeven_activated and not reached_tp1 and l_val <= tp1:
                                    reached_tp1 = True
                                    breakeven_activated = True
                                    sl_current = entry_price
                                # 3. Потім TP2
                                if l_val <= tp2:
                                    reached_tp2 = True
                                    break
                                    
                        # Оновлюємо безубиток у БД та надсилаємо повідомлення
                        if reached_tp1 and t.get("breakeven_activated", 0) == 0:
                            print(f"[{datetime.now()}] 🧠 Віртуальна угода {symbol}: досягнуто TP1 ({tp1}). Переводимо в безубиток.")
                            update_breakeven_status(symbol=symbol, order_id=order_id, status=1)
                            send_telegram_message(
                                f"🧠 <b>Virtual TP1 Reached (Breakeven)</b>\n"
                                f"Монета: <b>{symbol}</b> ({direction})\n"
                                f"Ціна входу: {entry_price} | TP1: {tp1}\n"
                                f"Віртуальний SL перенесено в безубиток."
                            )
                        
                        sl = sl_current
                        
                        # Розраховуємо пропорційний об'єм на основі ризику для віртуальних PnL
                        risk_pct = float(config.get("risk_pct", 1.0))
                        risk_pct = min(max(risk_pct, 0.1), 1.0)
                        risk_amount = free_usdt * (risk_pct / 100.0)
                        price_diff = abs(entry_price - sl)
                        if price_diff <= 0:
                            price_diff = entry_price * 0.02
                        virtual_qty = risk_amount / price_diff
                        
                        if reached_tp2 and not reached_sl:
                            status_outcome = "VIRTUAL_WIN" if is_virtual else "WIN"
                            exit_price = tp2
                            actual_pnl = virtual_qty * abs(tp2 - entry_price)
                        elif reached_sl:
                            status_outcome = "VIRTUAL_LOSS" if is_virtual else "LOSS"
                            exit_price = sl
                            actual_pnl = -virtual_qty * abs(entry_price - sl)
                        else:
                            if is_virtual:
                                # Віртуальна угода ще відкрита
                                return
                            status_outcome = "WIN"
                            exit_price = entry_price
                            actual_pnl = 0.0
                            
                        # Самонавчання для Квантового Ядра
                        factors_str = t.get("factors_snapshot")
                        if factors_str:
                            try:
                                factors_snap = json.loads(factors_str)
                                learned_outcome = "WIN" if "WIN" in status_outcome else "LOSS"
                                if factors_snap and learned_outcome in ("WIN", "LOSS"):
                                    learn_from_trade(factors_snap, learned_outcome, actual_pnl)
                                    # V11 Phase 8: Record for adaptive features (fallback/virtual path)
                                    try:
                                        import feature_manager
                                        is_win = (learned_outcome == "WIN")
                                        for fname in feature_manager.manager.features.keys():
                                            if feature_manager.manager.is_enabled(fname):
                                                feature_manager.manager.record_result(fname, is_win)
                                    except Exception:
                                        pass
                            except Exception as e_learn:
                                print(f"[{datetime.now()}] ⚠️ Не вдалося запустити learn_from_trade (fallback): {e_learn}")

                        update_trade_status(symbol=symbol, status=status_outcome, pnl=actual_pnl, order_id=order_id)
                        record_trade(symbol, direction, entry_price, exit_price, actual_pnl)
                        trade_prefix = "🧠 Віртуальна" if is_virtual else "🎯"
                        print(f"[{datetime.now()}] {trade_prefix} угода {symbol} закрита (fallback): {status_outcome} PnL={actual_pnl:.4f} USDT")
                        # V9.1: Progressive auto-blacklist after loss (fallback path)
                        if actual_pnl < 0:
                            _daily_loss_counter["count"] += 1
                            _loss_count = get_symbol_loss_count(symbol, hours=24)
                            if _loss_count >= 3:
                                ban_info = blacklist_symbol(symbol, f"{_loss_count} збитків за 24г")
                                send_telegram_message(
                                    f"🚫 <b>{symbol}</b> заблоковано на {ban_info['hours']}г "
                                    f"(рівень {ban_info['level']}): {_loss_count} збитків"
                                )
                except Exception as e_fallback:
                    print(f"[{datetime.now()}] ⚠️ Не вдалося синхронізувати угоду {symbol} через fallback: {e_fallback}")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(open_trades), 5)) as executor:
            executor.map(process_trade, open_trades)
    except Exception as e_sync:
        print(f"[{datetime.now()}] ⚠️ Помилка у sync_open_trades: {e_sync}")



def run_bot():
    # Запускаємо Health-сервер для Render
    start_health_server()
    
    # Ініціалізуємо БД
    init_db()
    import feature_manager
    
    startup_config = load_dynamic_config()
    max_daily_loss_pct = float(startup_config.get("max_daily_loss_pct", MAX_DAILY_LOSS_PCT))
    max_session_drawdown_pct = float(startup_config.get("max_session_drawdown_pct", MAX_SESSION_DRAWDOWN_PCT))
    require_confirmation = bool(startup_config.get("require_confirmation", False))
    confirmation_timeout_sec = int(startup_config.get("confirmation_timeout_sec", 120))
    pending_signals = {}
    pending_keys = set()
    pending_lock = threading.Lock()
    exchange = init_bybit(startup_config)
    start_bal_info = safe_api_call(exchange.fetch_balance) or {}
    session_start_equity = extract_usdt_equity(start_bal_info)
    day_start_equity = session_start_equity
    print(f"[{datetime.now()}] 💰 Стартовий equity сесії: {session_start_equity:.4f} USDT")
    
    # Завантажуємо повний список доступних символів один раз
    all_symbols = get_all_usdt_symbols(exchange)
    cycle_index = 0
    filter_manager = AdaptiveFilterManager()
    day_marker = None
    last_daily_report_day = None

    leverage = startup_config.get('leverage', 3.0)
    risk_pct = startup_config.get('risk_pct', 0.5)
    send_telegram_message(
        f"🚀 <b>SMC Racer (Мульти-Агентна версія)</b> активована!\n"
        f"Режим: <b>DEMO TRADING</b> (Плече {leverage}x, Ризик {risk_pct}%)\n"
        f"Доступно пар на Bybit: <b>{len(all_symbols)}</b>.\n"
        f"Очікую сигнали..."
    )
    print(f"[{datetime.now()}] Бот запущений. Доступно {len(all_symbols)} пар на демо рахунку.")

    if TELEGRAM_BOT_TOKEN:
        def _tg_callback_loop():
            consecutive_failures = 0
            while True:
                with pending_lock:
                    success = poll_telegram_callbacks(TELEGRAM_BOT_TOKEN, pending_signals)
                if success:
                    consecutive_failures = 0
                    time.sleep(0.8)
                else:
                    consecutive_failures += 1
                    sleep_time = min(30.0, 0.8 * (2 ** min(consecutive_failures, 10)))
                    time.sleep(sleep_time)
        threading.Thread(target=_tg_callback_loop, name="tg-callback-poller", daemon=True).start()
        print(f"[{datetime.now()}] ✅ Telegram callback poller запущено в окремому потоці (з адаптивним бекоффом)")

    # Запускаємо перший цикл консенсусу ШІ-агентів через 10 секунд після старту
    last_agents_run = time.time() - 86000 # Запустить через 40 секунд після запуску бота
    dry_cycles_without_setups = 0
    last_health_ping = time.time()
    last_ai_signal_ping = time.time()
    
    # V9.0: Daily loss tracking & reflection pause
    daily_loss_count = 0
    daily_loss_limit = 3
    daily_loss_halt = False
    reflection_pause_until = 0
    
    # V10: Vision AI Cache {symbol: {"decision": str, "timestamp": float}}
    _vision_cache = {}
    
    # V11.1: WR-based CHOP throttle
    _chop_throttle_until = 0.0
    def _check_recent_wr():
        """Перевіряє WR за останні 2 години. Якщо < 50%, повертає True (тротл)."""
        try:
            from db_logger import get_db_conn
            with get_db_conn() as conn:
                row = conn.execute(
                    "SELECT SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as w, COUNT(*) as t "
                    "FROM trades WHERE status IN ('WIN','LOSS','VIRTUAL_WIN','VIRTUAL_LOSS') "
                    "AND timestamp >= datetime('now', '-2 hours')"
                ).fetchone()
                if row and row[1] and row[1] >= 6:
                    wr = (row[0] or 0) / row[1]
                    if wr < 0.50:
                        print(f"[{datetime.now()}] ⚠️ WR за 2 години: {wr*100:.0f}% ({row[0] or 0}W/{row[1]}T) — тротлінг 10хв")
                        return True
        except Exception:
            pass
        return False

    while True:
        # Динамічно завантажуємо конфігурацію на початку кожного сканування
        base_config = load_dynamic_config()
        CONFIG = build_runtime_config(base_config, dry_cycles_without_setups)
        active_filters = filter_manager.get_filters()
        CONFIG["adx_min"] = active_filters["adx"]
        CONFIG["vol_multiplier_min"] = active_filters["vol"]
        CONFIG["vol_mult"] = min(float(CONFIG.get("vol_mult", 1.0)), float(active_filters["vol"]))
        CONFIG["fvg_min_size"] = active_filters["fvg"]
        # FIXED: max drawdown + daily loss guard перед скануванням/ордерами.
        bal = safe_api_call(exchange.fetch_balance) or {}
        equity_now = extract_usdt_equity(bal, fallback=session_start_equity)
        today = datetime.now(timezone.utc).date()
        if day_marker != today:
            day_marker = today
            day_start_equity = equity_now
            session_start_equity = equity_now
            last_daily_report_day = day_marker
            # V9.0: Reset daily loss tracking
            daily_loss_count = 0
            daily_loss_halt = False
            _daily_loss_counter["count"] = 0
            _daily_loss_counter["day"] = today
            print(f"[{datetime.now()}] 🔄 Новий день — лічильники збитків скинуто")
        session_dd = ((session_start_equity - equity_now) / max(session_start_equity, 1.0)) * 100.0
        daily_dd = ((day_start_equity - equity_now) / max(day_start_equity, 1.0)) * 100.0
        if session_dd >= max_session_drawdown_pct or daily_dd >= max_daily_loss_pct:
            print(f"[{datetime.now()}] 🛑 Risk guard stop: session_dd={session_dd:.2f}% daily_dd={daily_dd:.2f}%")
            time.sleep(60)
            continue
        
        # V9.1: Per-symbol progressive blacklist replaces global halt
        # (Global halt removed — blocking is per-symbol, not per-portfolio)
        
        # V9.0: Non-blocking reflection pause
        if time.time() < reflection_pause_until:
            remaining = int(reflection_pause_until - time.time())
            print(f"[{datetime.now()}] ⏸️ Reflection pause: ще {remaining}с до відновлення")
            time.sleep(60)
            continue
        
        # V11.1: WR-based CHOP throttle — пауза при поганому WR
        if time.time() < _chop_throttle_until:
            remaining = int(_chop_throttle_until - time.time())
            print(f"[{datetime.now()}] ⏸️ CHOP throttle: ще {remaining}с до відновлення")
            sync_open_trades(exchange, CONFIG)
            time.sleep(60)
            continue
        if _check_recent_wr():
            _chop_throttle_until = time.time() + 600  # 10 хвилин паузи
            send_telegram_message(
                f"⚠️ <b>CHOP Throttle активовано</b>\n"
                f"WR за 2 години < 50% — пауза 10 хвилин.\n"
                f"Синхронізація існуючих угод продовжується."
            )
            sync_open_trades(exchange, CONFIG)
            time.sleep(60)
            continue
        
        # V9.0: Regime Filter — перевіряємо BTC перед скануванням пар
        try:
            regime_result = check_market_regime(exchange)
            if not regime_result["allow_trading"]:
                regime_name = regime_result['regime']
                if regime_name == "MANIPULATION":
                    # MANIPULATION — повна зупинка нових входів
                    print(f"[{datetime.now()}] 🛑 Regime: MANIPULATION — нові входи повністю заблоковано")
                    send_telegram_message(
                        f"🛑 <b>Regime: MANIPULATION</b>\n"
                        f"{regime_result['details']}\n"
                        f"Нові входи заблоковано. Чекаємо 2 хвилини."
                    )
                    sync_open_trades(exchange, CONFIG)
                    time.sleep(120)
                    continue
                else:
                    # CHOP / VOLATILE — посилюємо фільтри, але НЕ блокуємо повністю
                    print(f"[{datetime.now()}] ⚠️ Regime: {regime_name} — посилюємо фільтри (торгівля дозволена з обмеженнями)")
                    # Посилюємо ADX поріг на +5, vol на ×1.3, підвищуємо auto_threshold
                    CONFIG["adx_min"] = max(float(CONFIG.get("adx_min", 12)), 20.0)
                    CONFIG["vol_multiplier_min"] = max(float(CONFIG.get("vol_multiplier_min", 0.7)), 1.0)
                    CONFIG["auto_execute_confidence_threshold"] = max(
                        float(CONFIG.get("auto_execute_confidence_threshold", 0.70)), 0.80
                    )
        except Exception as e_regime:
            print(f"[{datetime.now()}] ⚠️ Помилка Regime Filter: {e_regime}")
        
        # V10: Direction Bias — зберігаємо для фільтрації контр-трендових сетапів
        try:
            _direction_bias = regime_result.get("direction_bias", "NEUTRAL")
        except Exception:
            _direction_bias = "NEUTRAL"
        
        # V9.0: Cleanup expired blacklist entries once per cycle
        try:
            cleanup_expired_blacklist()
        except Exception:
            pass
            
        # V8.0: Autonomous Reflection Agent
        try:
            import pnl_tracker
            import reflection_agent
            consec_losses = pnl_tracker.get_consecutive_losses()
            if consec_losses >= 3:
                total_trades = len(pnl_tracker.load_stats().get("trades", []))
                last_reflected_count = getattr(reflection_agent, "_last_reflected_count", 0)
                
                # Check if we have new closed trades since the last reflection to prevent endless pause loops
                if total_trades > last_reflected_count:
                    ctx = pnl_tracker.get_recent_trades_context(limit=consec_losses)
                    reflection_agent._last_reflected_count = total_trades
                    analysis = reflection_agent.ask_kimi_reflection(ctx)
                    regime = analysis.get("regime", "UNKNOWN")
                    rec = analysis.get("recommendation", "")
                    msg = f"🧠 *AI Рефлексія (Збитки: {consec_losses})*\nРинок: {regime}\nПорада: {rec}"
                    print(msg)
                    send_telegram_message(msg)
                    
                    if regime in ["CHOP", "VOLATILE", "MANIPULATION"]:
                        reflection_pause_until = time.time() + 1800  # 30 хвилин non-blocking
                        print(f"[{datetime.now()}] 🛑 AI зупиняє торгівлю через {regime} на 30 хв (non-blocking).")
                        send_telegram_message(f"⏸️ Reflection pause: {regime}. Пауза 30 хв.")
                        # НЕ continue — дозволяємо sync_open_trades виконатися
        except Exception as e_refl:
            print(f"[{datetime.now()}] ⚠️ Помилка виклику Reflection Agent: {e_refl}")

        # Щоденний звіт о 23:59 UTC
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour == 23 and now_utc.minute >= 59:
            if last_daily_report_day != now_utc.date():
                summary = get_summary()
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": f"🌙 *Підсумок дня*\n{summary}",
                        "parse_mode": "Markdown",
                    },
                    timeout=10,
                )
                last_daily_report_day = now_utc.date()
        def process_pending_confirmations():
            """Швидка обробка pending сигналів без очікування завершення всього циклу сканування."""
            if not (require_confirmation and TELEGRAM_BOT_TOKEN):
                return
            with pending_lock:
                snapshot_pending = list(pending_signals.items())
            for sid, pending in snapshot_pending:
                if pending.get("approved") is True:
                    sig = pending["signal"]
                    direction = sig["direction"]
                    symbol = sig["symbol"]
                    symbol_open_orders = get_open_orders(exchange, symbol)
                    if has_same_direction_open_order(symbol_open_orders, direction):
                        send_telegram_message(
                            f"ℹ️ <b>{symbol}</b>: вже є відкритий <b>{direction}</b> ордер на біржі — новий не створюю."
                        )
                        with pending_lock:
                            pending_signals.pop(sid, None)
                            pending_keys.discard((symbol, direction))
                        continue
                    positions = get_open_positions(exchange)
                    open_orders = get_open_orders(exchange)
                    
                    # Перевіряємо дублікат
                    has_duplicate = any(p.get("symbol") == symbol for p in positions)
                            
                    if has_duplicate:
                        with pending_lock:
                            pending_signals.pop(sid, None)
                            pending_keys.discard((symbol, direction))
                        continue
                        
                    # Перевіряємо ліміт активних позицій та ордерів окремо
                    max_positions = int(CONFIG.get("max_concurrent_positions", 15))
                    max_orders = int(CONFIG.get("max_active_orders", 15))
                    
                    # V10: Smart Retry (Probation)
                    import db_logger
                    loss_count_24h = db_logger.get_symbol_loss_count(symbol, 24)
                    is_probation = (loss_count_24h >= 2)
                    
                    if len(positions) >= max_positions or len(open_orders) >= max_orders or is_probation:
                        reason_msg = "Портфель заповнений" if not is_probation else "Smart Retry (Probation)"
                        print(f"[{datetime.now()}] 🧠 {reason_msg}. Відкриваємо ВІРТУАЛЬНУ позицію для підтвердженого {symbol}")
                        log_trade(
                            symbol=symbol,
                            direction=direction,
                            entry=sig["entry"],
                            sl=sig["sl"],
                            tp1=sig["tp1"],
                            tp2=sig["tp2"],
                            fib=CONFIG.get("fib_level", 0.5),
                            sl_mult=CONFIG.get("sl_atr_mult", 1.5),
                            order_id=f"VIRTUAL_{symbol}_{int(time.time())}",
                            quant_score=sig.get("quant_score"),
                            factors_snapshot=sig.get("factors_snapshot"),
                        )
                        send_telegram_message(
                            f"🧠 <b>Virtual Position Opened (Confirmed)</b>\n"
                            f"Монета: <b>{symbol}</b> (<i>{direction}</i>)\n"
                            f"Вхід: {sig['entry']:.4f} | SL: {sig['sl']:.4f} | TP2: {sig['tp2']:.4f}"
                        )
                        with pending_lock:
                            pending_signals.pop(sid, None)
                            pending_keys.discard((symbol, direction))
                        continue
                        
                    if can_open_position(symbol, direction, positions, open_orders, CONFIG, notify_tg=True):
                        print(f"[{datetime.now()}] 📤 Підтверджений сигнал, відправляємо ордер на DEMO: {symbol} {direction}")
                        order = execute_demo_order(
                            exchange=exchange,
                            symbol=symbol,
                            direction=direction,
                            entry=sig["entry"],
                            sl=sig["sl"],
                            tp1=sig["tp1"],
                            tp2=sig["tp2"],
                            risk_pct=calculate_kelly_risk(float(CONFIG["risk_pct"]), sig.get("quant_score", 0.65)),
                            leverage=CONFIG.get("leverage", 3.0),
                            max_position_notional_pct=CONFIG.get("max_position_notional_pct", 30.0),
                        )
                        if order:
                            log_trade(
                                symbol=symbol,
                                direction=direction,
                                entry=sig["entry"],
                                sl=sig["sl"],
                                tp1=sig["tp1"],
                                tp2=sig["tp2"],
                                fib=CONFIG.get("fib_level", 0.5),
                                sl_mult=CONFIG.get("sl_atr_mult", 1.5),
                                order_id=order.get("id"),
                                quant_score=sig.get("quant_score"),
                                factors_snapshot=sig.get("factors_snapshot"),
                            )
                        if order and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                            qty_for_tg = order.get("amount") or order.get("qty") or order.get("_bot_amount") or sig.get("qty") or "N/A"
                            send_position_opened(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, {
                                "symbol": symbol,
                                "side": "Buy" if direction == "LONG" else "Sell",
                                "entry_price": sig["entry"],
                                "qty": qty_for_tg,
                                "sl": sig["sl"],
                                "tp": sig["tp2"],
                            })
                            record_event("order_opened", {"symbol": symbol, "direction": direction, "source": "confirmed"})
                        elif order is None:
                            record_event("order_rejected", {"symbol": symbol, "direction": direction, "source": "confirmed"})
                    with pending_lock:
                        pending_signals.pop(sid, None)
                        pending_keys.discard((symbol, direction))
                    continue
                if pending.get("approved") is False:
                    sig = pending.get("signal", {})
                    record_event("skip", {"symbol": sig.get("symbol"), "direction": sig.get("direction")})
                    with pending_lock:
                        pending_signals.pop(sid, None)
                        pending_keys.discard((sig.get("symbol"), sig.get("direction")))
                    continue
                if (time.time() - pending.get("created_at", time.time())) > confirmation_timeout_sec:
                    sig = pending.get("signal", {})
                    with pending_lock:
                        pending_signals.pop(sid, None)
                        pending_keys.discard((sig.get("symbol"), sig.get("direction")))
                    send_telegram_message(f"⌛ Сигнал скасовано по таймауту: {pending['signal']['symbol']}")
                    continue

        process_pending_confirmations()
        # Синхронізація угод та скасування застарілих ордерів
        sync_open_trades(exchange, CONFIG)
        cancel_stale_orders(exchange, CONFIG)

        cycle_scanned = 0
        cycle_setups = 0
        cycle_invalid_symbols = 0
        cycle_rate_limits = 0
        pairs_with_enough_data = 0
        pairs_with_valid_adx = 0
        pairs_filtered_by_adx = 0
        adx_fail = 0
        vol_fail = 0
        fvg_fail = 0
        passed_all = 0
        debug_logged = False
        
        diag_pair = None
        diag_margin = 1e9
        diag_block = {}

        max_symbols = max(1, int(CONFIG.get("max_symbols", 120)))
        symbol_offset = int(CONFIG.get("symbol_offset", 0))
        symbols_count = len(all_symbols)
        if symbols_count == 0:
            print(f"[{datetime.now()}] ⚠️ Немає символів для сканування")
            time.sleep(60)
            continue

        normalized_offset = symbol_offset % symbols_count
        rotated_symbols = all_symbols[normalized_offset:] + all_symbols[:normalized_offset]
        symbol_window = rotated_symbols[:max_symbols]

        global last_setup_bars
        for sym in symbol_window:
            if sym not in last_setup_bars:
                last_setup_bars[sym] = None

        cycle_symbols = select_symbols_for_scan(exchange, symbol_window, CONFIG, cycle_index)

        print(f"[{datetime.now()}] ⚡ Асинхронне завантаження OHLCV для {len(cycle_symbols)} пар...")
        try:
            from async_scanner import get_market_data_parallel
            parallel_data = get_market_data_parallel(cycle_symbols, [(TIMEFRAME, 100), ("4h", 50)])
            data_15m = parallel_data.get(TIMEFRAME, {})
            data_4h = parallel_data.get("4h", {})
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Помилка асинхронного завантаження: {e}. Пропускаємо цикл.")
            time.sleep(10)
            continue

        for symbol in cycle_symbols:
            # Критично для швидкості: не чекаємо кінця циклу, обробляємо підтвердження одразу між парами.
            process_pending_confirmations()
            try:
                # 1. Формуємо DataFrame з асинхронно завантажених даних
                raw_15m = data_15m.get(symbol)
                raw_4h = data_4h.get(symbol)
                
                if not raw_15m or not raw_4h:
                    continue
                    
                df = pd.DataFrame(raw_15m, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                
                htf_df = pd.DataFrame(raw_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
                htf_df["timestamp"] = pd.to_datetime(htf_df["timestamp"], unit="ms", utc=True)
                
                # V11: Downcast float64 → float32 (зменшує RAM ~50%)
                for _col in ["open", "high", "low", "close", "volume"]:
                    df[_col] = df[_col].astype(np.float32)
                    htf_df[_col] = htf_df[_col].astype(np.float32)
                
                if len(df) < MIN_CANDLES_REQUIRED or len(htf_df) < MIN_CANDLES_REQUIRED:
                    print(f"[{datetime.now()}] ⚠️ Пропускаємо {symbol}: мало свічок")
                    continue
                pairs_with_enough_data += 1
                cycle_scanned += 1

                # 2. Проганяємо логіку Racer
                states = analyze_racer(df, htf_df, CONFIG)
                last_state = states[-1]
                if not pd.isna(getattr(last_state, "adx", np.nan)):
                    pairs_with_valid_adx += 1
                    adx_v = float(last_state.adx)
                    adx_t = float(getattr(last_state, "adx_threshold", CONFIG.get("adx_min", 12)))
                    vol_v = float(getattr(last_state, "rel_vol", 0.0))
                    vol_t = float(CONFIG.get("vol_multiplier_min", CONFIG.get("vol_mult", 1.0)))
                    fvg_v = float(getattr(last_state, "fvg_size_atr", 0.0))
                    fvg_t = float(CONFIG.get("fvg_min_size", 0.08))
                    
                    margin = abs(adx_t - adx_v)
                    if margin < diag_margin:
                        diag_margin = margin
                        diag_pair = symbol
                        diag_block = {"adx": adx_v, "adx_t": adx_t, "vol": vol_v, "vol_t": vol_t, "fvg": fvg_v, "fvg_t": fvg_t}
                        
                    if adx_v < adx_t:
                        pairs_filtered_by_adx += 1
                        adx_fail += 1
                        if not debug_logged:
                            print(f"[{datetime.now()}] DEBUG {symbol}: ADX={adx_v:.2f} < {adx_t:.2f} ❌")
                            debug_logged = True
                    elif vol_v < vol_t:
                        vol_fail += 1
                        if not debug_logged:
                            print(f"[{datetime.now()}] DEBUG {symbol}: VOL={vol_v:.2f} < {vol_t:.2f} ❌")
                            debug_logged = True
                    elif fvg_v < fvg_t:
                        fvg_fail += 1
                        if not debug_logged:
                            print(f"[{datetime.now()}] DEBUG {symbol}: FVG={fvg_v:.4f} < {fvg_t:.4f} ❌")
                            debug_logged = True
                    else:
                        passed_all += 1
                        if not debug_logged:
                            print(f"[{datetime.now()}] DEBUG {symbol}: всі фільтри ✅ але сетап не знайдено")
                            debug_logged = True

                # 3. Перевіряємо сетап
                if symbol in DEBUG_PAIRS:
                    setup_found = bool(last_state.setup and last_state.setup.valid)
                    print(f"[{datetime.now()}] --- DEBUG {symbol} ---")
                    print(f"[{datetime.now()}]   BOS bull: {getattr(last_state, 'bos_bull', False)} | BOS bear: {getattr(last_state, 'bos_bear', False)}")
                    print(f"[{datetime.now()}]   CHoCH bull/bear: {getattr(last_state, 'choch_bull', False)}/{getattr(last_state, 'choch_bear', False)}")
                    print(f"[{datetime.now()}]   OB active: {getattr(last_state, 'ob_active', False)}")
                    print(f"[{datetime.now()}]   FVG bull naked: {getattr(last_state, 'bull_fvg', False)} | FVG bear naked: {getattr(last_state, 'bear_fvg', False)}")
                    print(f"[{datetime.now()}]   HTF trend: {'bull' if last_state.is_htf_bullish else 'bear' if last_state.is_htf_bearish else 'flat'}")
                    print(f"[{datetime.now()}]   Session: {getattr(last_state, 'session', 'Off')}")
                    print(f"[{datetime.now()}]   Impulse bull/bear: {getattr(last_state, 'is_impulse_bull', False)}/{getattr(last_state, 'is_impulse_bear', False)}")
                    print(f"[{datetime.now()}]   ATR: {getattr(last_state, 'atr', float('nan')):.4f}")
                    print(f"[{datetime.now()}]   Final decision (setup_found): {setup_found}")

                if last_state.setup and last_state.setup.valid:
                    # V9.0: Blacklist check before processing setup
                    if is_blacklisted(symbol):
                        print(f"[{datetime.now()}] 🚫 {symbol} у чорному списку — пропускаємо сетап")
                        continue
                    # V11.1: Direction Bias — дозволяємо SHORT/LONG, але з підвищеним порогом
                    setup_dir = "LONG" if last_state.setup.dir == 1 else "SHORT"
                    _counter_trend = False
                    if _direction_bias == "BULLISH" and setup_dir == "SHORT":
                        _counter_trend = True
                        print(f"[{datetime.now()}] ⚠️ Контр-тренд: BULLISH ринок + SHORT {symbol} — підвищуємо поріг")
                    elif _direction_bias == "BEARISH" and setup_dir == "LONG":
                        _counter_trend = True
                        print(f"[{datetime.now()}] ⚠️ Контр-тренд: BEARISH ринок + LONG {symbol} — підвищуємо поріг")
                    if last_setup_bars[symbol] != last_state.timestamp:
                        adx_v = float(getattr(last_state, "adx", 0.0) or 0.0)
                        adx_t = float(getattr(last_state, "adx_threshold", CONFIG.get("adx_min", 12)))
                        vol_v = float(getattr(last_state, "rel_vol", 0.0) or 0.0)
                        vol_t = float(CONFIG.get("vol_multiplier_min", CONFIG.get("vol_mult", 1.0)))
                        born_idx = last_state.setup.born_bar
                        birth_bar = states[born_idx] if 0 <= born_idx < len(states) else last_state
                        fvg_v = float(getattr(birth_bar, "fvg_size_atr", 0.0) or 0.0)
                        fvg_t = float(CONFIG.get("fvg_min_size", 0.08))
                        if adx_v < adx_t or vol_v < vol_t or fvg_v < fvg_t:
                            reason = f"ADX {adx_v:.2f}/{adx_t:.2f}, VOL {vol_v:.2f}/{vol_t:.2f}, FVG {fvg_v:.4f}/{fvg_t:.4f}"
                            record_event("setup_blocked_by_runtime_filters", {"symbol": symbol, "reason": reason})
                            print(f"[{datetime.now()}] 🧱 Сетап {symbol} заблоковано runtime-фільтрами: {reason}")
                            continue

                        setup = last_state.setup
                        direction = "LONG" if setup.dir == 1 else "SHORT"
                        levels_ok, levels_reason = validate_trade_levels(direction, setup.entry, setup.sl, setup.tp1, setup.tp2)
                        if not levels_ok:
                            record_event("invalid_levels", {"symbol": symbol, "direction": direction, "reason": levels_reason})
                            print(f"[{datetime.now()}] ⛔ Некоректний сетап {symbol}: {levels_reason}")
                            continue

                        last_setup_bars[symbol] = last_state.timestamp
                        cycle_setups += 1
                        direction_str = "LONG 🟢" if setup.dir == 1 else "SHORT 🔴"
                        
                        msg = format_signal({
                            "direction": direction,
                            "symbol": symbol,
                            "entry": setup.entry,
                            "sl": setup.sl,
                            "tp1": setup.tp1,
                            "tp2": setup.tp2,
                            "atr": getattr(last_state, "atr", "N/A"),
                        })
                        print(f"[{datetime.now()}] {msg.replace('<b>', '').replace('</b>', '')}")
                        
                        signal_payload = {
                            "direction": direction,
                            "symbol": symbol,
                            "entry": setup.entry,
                            "sl": setup.sl,
                            "tp1": setup.tp1,
                            "tp2": setup.tp2,
                            "atr": getattr(last_state, "atr", 0),
                        }
                        
                        # --- Квантове Оцінювання Сетапу (Quant Scoring Engine) ---
                        print(f"[{datetime.now()}] 🤖 Квантове оцінювання для {symbol}...")
                        
                        # Отримуємо додаткові дані для скорингу
                        adx_v = float(getattr(last_state, "adx", 0.0) or 0.0)
                        adx_t = float(getattr(last_state, "adx_threshold", CONFIG.get("adx_min", 12)))
                        vol_v = float(getattr(last_state, "rel_vol", 0.0) or 0.0)
                        vol_t = float(CONFIG.get("vol_multiplier_min", CONFIG.get("vol_mult", 1.0)))
                        born_idx = last_state.setup.born_bar
                        birth_bar = states[born_idx] if 0 <= born_idx < len(states) else last_state
                        fvg_v = float(getattr(birth_bar, "fvg_size_atr", 0.0) or 0.0)
                        fvg_t = float(CONFIG.get("fvg_min_size", 0.08))
                        atr_v = float(getattr(last_state, "atr", 0.0) or 0.0)
                        
                        is_htf_bullish = bool(getattr(last_state, "is_htf_bullish", False))
                        is_htf_bearish = bool(getattr(last_state, "is_htf_bearish", False))
                        session_v = str(getattr(last_state, "session", "Off"))
                        bos_bull_v = bool(getattr(last_state, "bos_bull", False))
                        bos_bear_v = bool(getattr(last_state, "bos_bear", False))
                        choch_bull_v = bool(getattr(last_state, "choch_bull", False))
                        choch_bear_v = bool(getattr(last_state, "choch_bear", False))
                        ob_active_v = bool(getattr(last_state, "ob_active", False))
                        bull_fvg_v = bool(getattr(last_state, "bull_fvg", False))
                        bear_fvg_v = bool(getattr(last_state, "bear_fvg", False))
                        is_impulse_bull_v = bool(getattr(last_state, "is_impulse_bull", False))
                        is_impulse_bear_v = bool(getattr(last_state, "is_impulse_bear", False))

                        quant_res = score_setup(
                            entry=setup.entry,
                            sl=setup.sl,
                            tp1=setup.tp1,
                            tp2=setup.tp2,
                            direction=direction,
                            adx=adx_v,
                            adx_threshold=adx_t,
                            rel_vol=vol_v,
                            vol_threshold=vol_t,
                            fvg_size_atr=fvg_v,
                            fvg_min=fvg_t,
                            atr=atr_v,
                            is_htf_bullish=is_htf_bullish,
                            is_htf_bearish=is_htf_bearish,
                            session=session_v,
                            bos_bull=bos_bull_v,
                            bos_bear=bos_bear_v,
                            choch_bull=choch_bull_v,
                            choch_bear=choch_bear_v,
                            ob_active=ob_active_v,
                            bull_fvg=bull_fvg_v,
                            bear_fvg=bear_fvg_v,
                            is_impulse_bull=is_impulse_bull_v,
                            is_impulse_bear=is_impulse_bear_v,
                            symbol=symbol,
                        )
                        
                        conf = quant_res["score"]
                        rationale = quant_res["rationale"]
                        factors_snapshot = quant_res["factors"]
                        auto_thresh = float(CONFIG.get("auto_execute_confidence_threshold", 0.65))
                        # V11.1: Контр-трендові угоди потребують вищий score
                        if _counter_trend:
                            auto_thresh = min(0.95, auto_thresh + 0.10)
                            print(f"[{datetime.now()}] ⚠️ Контр-тренд поріг: {auto_thresh:.2f} для {symbol}")
                        
                        # V11: ЖОРСТКИЙ ГЕЙТ — перевіряємо RAW score ПЕРЕД Vision AI та Telegram
                        if conf < auto_thresh:
                            print(f"[{datetime.now()}] 🚫 Score {conf:.2f} < {auto_thresh:.2f} — {symbol} ЗАБЛОКОВАНО (hard gate)")
                            record_event("setup_blocked_by_hard_gate", {"symbol": symbol, "score": conf, "threshold": auto_thresh})
                            continue
                        
                        # --- V11: Vision AI Filter (ПІСЛЯ hard gate, ДО Telegram) ---
                        try:
                            import vision_agent
                            now_ts = time.time()
                            cached = _vision_cache.get(symbol)
                            
                            # V11: Ліміт vision cache (OOM prevention)
                            if len(_vision_cache) > 200:
                                oldest = sorted(_vision_cache, key=lambda k: _vision_cache[k]["timestamp"])[:100]
                                for k in oldest:
                                    del _vision_cache[k]
                            
                            if cached and (now_ts - cached["timestamp"] < 900):
                                vision_decision = cached["decision"]
                                print(f"[{datetime.now()}] 👁️ Vision AI (CACHED) для {symbol}: {vision_decision}")
                            else:
                                print(f"[{datetime.now()}] 👁️ Перевірка {symbol} через Vision AI (PaliGemma)...")
                                vision_decision = vision_agent.ask_vision_oracle(df, symbol)
                                _vision_cache[symbol] = {"decision": vision_decision, "timestamp": now_ts}

                            if (direction == "LONG" and vision_decision == "BEARISH") or (direction == "SHORT" and vision_decision == "BULLISH"):
                                print(f"[{datetime.now()}] 🚫 Vision AI заблокував {direction} для {symbol}. Зображення вказує на {vision_decision}.")
                                record_event("setup_blocked_by_vision_ai", {"symbol": symbol, "vision": vision_decision})
                                continue
                            elif (direction == "LONG" and vision_decision == "BULLISH") or (direction == "SHORT" and vision_decision == "BEARISH"):
                                conf = min(0.95, conf * 1.05) # V11: Мультиплікатор замість +0.15
                                rationale += f" | 👁️ Vision AI підтверджує {vision_decision}."
                                factors_snapshot["vision_score"] = round(conf - quant_res["score"], 3)
                            else:
                                rationale += f" | 👁️ Vision AI: NEUTRAL."
                                factors_snapshot["vision_score"] = 0.0
                        except Exception as e_vision:
                            print(f"[{datetime.now()}] ⚠️ Помилка Vision AI: {e_vision}")

                        
                        signal_payload["quant_score"] = conf
                        signal_payload["factors_snapshot"] = factors_snapshot
                        signal_payload["ai_confidence"] = conf
                        signal_payload["ai_rationale"] = rationale
                        
                        is_auto_execute = conf >= auto_thresh
                        
                        if is_auto_execute and TELEGRAM_BOT_TOKEN:
                            safe_rationale = str(rationale).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                            send_telegram_message(
                                f"⚡ <b>Quant Auto-Execute ({conf*100:.0f}%)</b>\n"
                                f"<b>{symbol}</b> {direction_str}\n"
                                f"<i>{safe_rationale}</i>"
                            )
                        
                        # Сповіщення в Telegram
                        if require_confirmation and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and not is_auto_execute:
                            signal_id = f"{symbol}-{int(time.time())}"
                            signal_key = (signal_payload["symbol"], signal_payload["direction"])
                            symbol_open_orders = get_open_orders(exchange, symbol)
                            if has_same_direction_open_order(symbol_open_orders, signal_payload["direction"]):
                                record_event("order_already_exists", {"symbol": symbol, "direction": signal_payload["direction"], "phase": "enqueue"})
                                send_telegram_message(
                                    f"ℹ️ <b>{symbol}</b>: на біржі вже стоїть ордер у напрямку "
                                    f"<b>{signal_payload['direction']}</b> — повторний сигнал пропущено."
                                )
                                continue
                            with pending_lock:
                                if signal_key in pending_keys:
                                    continue
                                pending_signals[signal_id] = {"signal": signal_payload, "created_at": time.time(), "approved": None}
                                pending_keys.add(signal_key)
                            record_event("confirm", {"symbol": symbol, "direction": signal_payload["direction"], "phase": "request"})
                            send_signal_with_buttons(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, {**signal_payload, "id": signal_id})
                            continue
                        else:
                            send_signal(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, signal_payload)
                        
                        # Вхід на Демо рахунку Bybit
                        positions = get_open_positions(exchange)
                        open_orders = get_open_orders(exchange)
                        
                        # Перевіряємо дублікат
                        has_duplicate = any(p.get("symbol") == symbol for p in positions)
                                
                        if has_duplicate:
                            continue
                            
                        # Перевіряємо ліміт активних позицій та ордерів окремо
                        max_positions = int(CONFIG.get("max_concurrent_positions", 15))
                        max_orders = int(CONFIG.get("max_active_orders", 15))
                        
                        # V10: Smart Retry (Probation)
                        import db_logger
                        loss_count_24h = db_logger.get_symbol_loss_count(symbol, 24)
                        is_probation = (loss_count_24h >= 2) # Якщо 2+ збитки за 24г — тестуємо віртуально
                        
                        if len(positions) >= max_positions or len(open_orders) >= max_orders or is_probation:
                            # ВІРТУАЛЬНИЙ вхід
                            reason_msg = "Портфель заповнений" if not is_probation else "Smart Retry (Probation)"
                            print(f"[{datetime.now()}] 🧠 {reason_msg}. Відкриваємо ВІРТУАЛЬНУ позицію для {symbol}")
                            # V11.3: Дедуплікація — блокуємо повторний VIRTUAL для того ж символу
                            try:
                                already_virtual = any(
                                    t.get("symbol") == symbol and t.get("status") == "VIRTUAL_OPEN"
                                    for t in (get_open_trades() or [])
                                )
                                if already_virtual:
                                    print(f"[{datetime.now()}] 🔄 Virtual dedup: {symbol} вже має VIRTUAL_OPEN — пропускаємо")
                                    continue
                            except Exception as e_dedup:
                                print(f"[{datetime.now()}] ⚠️ Virtual dedup error (ignored): {e_dedup}")
                            log_trade(
                                symbol=symbol,
                                direction=direction,
                                entry=setup.entry,
                                sl=setup.sl,
                                tp1=setup.tp1,
                                tp2=setup.tp2,
                                fib=CONFIG.get("fib_level", 0.5),
                                sl_mult=CONFIG.get("sl_atr_mult", 1.5),
                                order_id=f"VIRTUAL_{symbol}_{int(time.time())}",
                                quant_score=conf,
                                factors_snapshot=factors_snapshot,
                            )
                            send_telegram_message(
                                f"🧠 <b>Virtual Position Opened</b>\n"
                                f"Монета: <b>{symbol}</b> ({direction_str})\n"
                                f"Вхід: {setup.entry:.4f} | SL: {setup.sl:.4f} | TP2: {setup.tp2:.4f}\n"
                                f"Оцінка: {conf*100:.0f}%"
                            )
                            continue

                        if CONFIG.get("dry_run", True):
                            print(f"[{datetime.now()}] 🧪 dry_run=true, ордер НЕ відправлено для {symbol}")
                        else:
                            order = execute_demo_order(
                                exchange=exchange,
                                symbol=symbol,
                                direction=direction,
                                entry=setup.entry,
                                sl=setup.sl,
                                tp1=setup.tp1,
                                tp2=setup.tp2,
                                risk_pct=calculate_kelly_risk(float(CONFIG["risk_pct"]), conf),
                                leverage=CONFIG.get("leverage", 3.0),
                                max_position_notional_pct=CONFIG.get("max_position_notional_pct", 30.0),
                            )
                            if order:
                                log_trade(
                                    symbol=symbol,
                                    direction=direction,
                                    entry=setup.entry,
                                    sl=setup.sl,
                                    tp1=setup.tp1,
                                    tp2=setup.tp2,
                                    fib=CONFIG.get("fib_level", 0.5),
                                    sl_mult=CONFIG.get("sl_atr_mult", 1.5),
                                    order_id=order.get("id"),
                                    quant_score=conf,
                                    factors_snapshot=factors_snapshot,
                                )
                            if order and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                                send_position_opened(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, {
                                    "symbol": symbol,
                                    "side": "Buy" if direction == "LONG" else "Sell",
                                    "entry_price": setup.entry,
                                    "qty": order.get("amount") or order.get("qty") or order.get("_bot_amount") or "N/A",
                                    "sl": setup.sl,
                                    "tp": setup.tp2,
                                })
                                record_event("order_opened", {"symbol": symbol, "direction": direction, "source": "auto"})
                            elif order is None:
                                record_event("order_rejected", {"symbol": symbol, "direction": direction, "source": "auto"})
                
                # Запобігання Rate Limit (динамічно від біржі)
                time.sleep(max(exchange.rateLimit / 1000.0, 0.35))

            except BadSymbol as e:
                cycle_invalid_symbols += 1
                print(f"[{datetime.now()}] ⚠️ Пропускаємо невалідний символ {symbol}: {e}")
                continue
            except RateLimitExceeded as e:
                cycle_rate_limits += 1
                print(f"[{datetime.now()}] ⚠️ Rate limit на {symbol}. Чекаємо 8с: {e}")
                time.sleep(8)
                continue
            except Exception as e:
                import traceback
                if "Symbol Is Invalid" in str(e):
                    cycle_invalid_symbols += 1
                    print(f"[{datetime.now()}] ⚠️ Пропускаємо невалідний символ {symbol}: {e}")
                    continue
                if "Too many visits" in str(e):
                    cycle_rate_limits += 1
                    print(f"[{datetime.now()}] ⚠️ Rate limit на {symbol}. Чекаємо 8с: {e}")
                    time.sleep(8)
                    continue
                print(f"[{datetime.now()}] ⚠️ Помилка на {symbol}: {e}")
                print(traceback.format_exc())
                time.sleep(1)
                continue


        if cycle_setups == 0:
            dry_cycles_without_setups += 1
        else:
            dry_cycles_without_setups = 0

        ladder_result = filter_manager.report_cycle(setups_found=cycle_setups)
        if ladder_result["changed"]:
            old = AdaptiveFilterManager.LADDER[ladder_result["old_level"]]["label"]
            new = AdaptiveFilterManager.LADDER[ladder_result["new_level"]]["label"]
            if ladder_result["new_level"] > ladder_result["old_level"]:
                print(f"[{datetime.now()}] 📉 ФІЛЬТРИ ЗНИЖЕНО: {old} → {new} (dry streak досяг ліміту)")
            else:
                print(f"[{datetime.now()}] 📈 ФІЛЬТРИ ПІДВИЩЕНО: {old} → {new} (стратегія знову працює)")
        if ladder_result["is_diagnostic"]:
            print(f"[{datetime.now()}] 🔬 DIAGNOSTIC MODE: ринок без чіткої структури. Збір даних без торгівлі.")
            # TODO: Тимчасово вимкнено через нестабільність AI API (504/timeout/None).
            pass

        print(
            f"[{datetime.now()}] 📊 Цикл завершено | scanned={cycle_scanned} setups={cycle_setups} "
            f"invalid={cycle_invalid_symbols} ratelimit={cycle_rate_limits} dry_cycles={dry_cycles_without_setups} "
            f"{filter_manager.get_status()}"
        )
        print(
            f"[{datetime.now()}] Статистика фільтрів: "
            f"ADX fail={adx_fail}/{len(cycle_symbols)} | "
            f"VOL fail={vol_fail}/{len(cycle_symbols)} | "
            f"FVG fail={fvg_fail}/{len(cycle_symbols)} | "
            f"Пройшли всі={passed_all}/{len(cycle_symbols)}"
        )
        record_cycle({
            "scanned": cycle_scanned,
            "setups": cycle_setups,
            "invalid": cycle_invalid_symbols,
            "ratelimit": cycle_rate_limits,
            "dry_cycles": dry_cycles_without_setups,
            "adx_fail": adx_fail,
            "vol_fail": vol_fail,
            "fvg_fail": fvg_fail,
            "passed_all": passed_all,
            "filter_level": filter_manager.get_status(),
        })
        if time.time() - last_health_ping > 7200:
            if diag_pair:
                adx_line = f"ADX: {diag_block['adx']:.2f} {'❌ менше' if diag_block['adx'] < diag_block['adx_t'] else '✅ ок'} поріг {diag_block['adx_t']:.2f}"
                vol_line = f"VOL: {diag_block['vol']:.2f} {'❌ менше' if diag_block['vol'] < diag_block['vol_t'] else '✅ ок'} поріг {diag_block['vol_t']:.2f}"
                fvg_line = f"FVG: {diag_block['fvg']:.2f} {'❌ менше' if diag_block['fvg'] < diag_block['fvg_t'] else '✅ ок'} поріг {diag_block['fvg_t']:.2f}"
            else:
                adx_line = vol_line = fvg_line = "n/a"
                
            send_telegram_message(
                f"🩺 <b>Heartbeat SMC Racer</b>\n"
                f"Скановано пар: <b>{cycle_scanned}</b>\n"
                f"Сетапів за цикл: <b>{cycle_setups}</b>\n"
                f"RateLimit помилок: <b>{cycle_rate_limits}</b>\n"
                f"Invalid symbols: <b>{cycle_invalid_symbols}</b>\n"
                f"Dry циклів поспіль: <b>{dry_cycles_without_setups}</b>\n"
                f"Пар з достатньо даних: <b>{pairs_with_enough_data}</b> / <b>{len(cycle_symbols)}</b>\n"
                f"Пар з валідним ADX: <b>{pairs_with_valid_adx}</b> / <b>{len(cycle_symbols)}</b>\n"
                f"Пар відфільтровано (ADX < поріг): <b>{pairs_filtered_by_adx}</b>\n"
                f"Фільтри зараз → ADX: <b>{CONFIG.get('adx_thresh')}</b>, VOL: <b>{CONFIG.get('vol_multiplier_min', CONFIG.get('vol_mult'))}</b>, FVG: <b>{CONFIG.get('fvg_min_size')}</b>\n"
                f"Остання пара з найближчим сетапом: <b>{diag_pair or 'n/a'}</b>\n"
                f"├── {adx_line}\n├── {vol_line}\n└── {fvg_line}"
            )
            print(f"[{datetime.now()}] {build_24h_report().replace('<b>', '').replace('</b>', '')}")
            last_health_ping = time.time()

        if cycle_setups == 0 and dry_cycles_without_setups >= 4 and (time.time() - last_ai_signal_ping > 1800):
            ai_signal = generate_ai_signal(exchange, cycle_symbols, TIMEFRAME)
            if ai_signal and ai_signal.get("direction") != "NONE":
                send_telegram_message(
                    f"🤖 <b>AI Advisory Signal</b>\n"
                    f"Символ: <b>{ai_signal.get('symbol','N/A')}</b>\n"
                    f"Напрямок: <b>{ai_signal.get('direction')}</b>\n"
                    f"Впевненість: <b>{ai_signal.get('confidence')}</b>\n"
                    f"Entry hint: <b>{ai_signal.get('entry_hint','-')}</b> | Stop hint: <b>{ai_signal.get('stop_hint','-')}</b>\n"
                    f"Причина: {ai_signal.get('rationale','-')}"
                )
                print(f"[{datetime.now()}] 🤖 AI advisory signal sent: {ai_signal}")
            last_ai_signal_ping = time.time()

        cycle_index += 1
        
        # --- V8.5 Persistent AI Memory: Auto-Evolution ---
        if cycle_index > 0 and cycle_index % 300 == 0:
            print(f"[{datetime.now()}] 🧬 Цикл {cycle_index}: Запуск фонової Генетичної Еволюції...")
            def _run_evo():
                try:
                    from genetic_algo import run_evolution
                    report = run_evolution(generations=5)
                    send_telegram_message(f"🧠 <b>Генетична Еволюція Завершена!</b>\n\n{report}")
                except Exception as e:
                    print(f"Помилка еволюції: {e}")
            threading.Thread(target=_run_evo, daemon=True).start()

        # Перевірка для чергового запуску ШІ-агентів (раз на 24 години)
        if time.time() - last_agents_run > 86400:
            try:
                from cooperative_agents import run_cooperative_agent_consensus
                run_cooperative_agent_consensus(exchange, symbol_window, TIMEFRAME, CONFIG_PATH)
                last_agents_run = time.time()
            except Exception as ae:
                print(f"Помилка запуску консенсусу агентів: {ae}")

        # V11 Phase 8: Adaptive Feature System - Evaluate features
        try:
            import feature_manager
            feature_manager.manager.evaluate_features()
        except Exception as ef:
            print(f"[{datetime.now()}] ⚠️ Помилка оцінки фіч: {ef}")

        # V10.2: Очищення пам'яті для уникнення OOM на Render
        import gc
        gc.collect()

        # Пауза 60 сек між повними колами сканування ринку
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
