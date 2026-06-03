import os
import time
import threading
import json
import ccxt
import numpy as np
import requests
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
from db_logger import init_db, log_trade, update_trade_status, get_open_trades, get_trade_by_order_id
from ai_signal_agent import generate_ai_signal
from adaptive_filters import AdaptiveFilterManager
from pnl_tracker import record_trade, get_summary
from ops_dashboard import record_cycle, record_event, build_24h_report
from logging_config import setup_file_logging

LOG_FILE = setup_file_logging("bot")

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'active_config.json')
TIMEFRAME = "15m"
MIN_CANDLES_REQUIRED = 50
DEBUG_PAIRS = {"BEAT/USDT:USDT", "BILL/USDT:USDT"}


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
        tickers = exchange.fetch_tickers(symbols)
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
                        with open(CONFIG_PATH, 'w') as wf:
                            json.dump(parsed, wf, ensure_ascii=False, indent=4)
                            wf.write("\n")
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
            
        # FIXED: жорсткий cap ризику на угоду <=1%.
        risk_pct = min(max(float(risk_pct), 0.1), 1.0)
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
        order = safe_api_call(
            exchange.create_order,
            symbol=symbol,
            type='limit',
            side=side,
            amount=float(qty_str),
            price=float(price_str),
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
            f"🛒 <b>ОРДЕР ВИСТАВЛЕНО НА DEMO</b>\n"
            f"Монета: <b>{symbol}</b> | Напрямок: <b>{direction}</b>\n"
            f"Вхід (Limit): <b>{price_str}</b>\n"
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


def can_open_position(symbol: str, direction: str, open_positions: list, open_orders: list, config: dict) -> bool:
    # 1. Check if a position in the same direction is already open for this symbol
    symbol_positions = [p for p in open_positions if p.get("symbol") == symbol]
    side_target = "buy" if direction == "LONG" else "sell"
    for p in symbol_positions:
        side = str(p.get("side") or p.get("info", {}).get("side", "")).lower()
        if side in {"buy", "long"} and side_target == "buy":
            print(f"[{datetime.now()}] ⛔ LONG вже відкрито для {symbol} — пропускаємо")
            return False
        if side in {"sell", "short"} and side_target == "sell":
            print(f"[{datetime.now()}] ⛔ SHORT вже відкрито для {symbol} — пропускаємо")
            return False
            
    # 2. Check global portfolio max concurrent positions + active limit orders limit
    max_positions = int(config.get("max_concurrent_positions", 5))
    total_active = len(open_positions) + len(open_orders)
    if total_active >= max_positions:
        print(f"[{datetime.now()}] ⛔ Досягнуто ліміт активних позицій/ордерів портфеля ({total_active}/{max_positions}) — пропускаємо")
        return False
        
    return True


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
                safe_api_call(exchange.cancel_order, order_id, symbol)
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
                    safe_api_call(exchange.cancel_order, order_id, symbol)
                    send_telegram_message(f"🛑 <b>Скасовано ордер (SL пробито до входу)</b>\nМонета: <b>{symbol}</b>\nЦіна: {current_price} | SL: {sl}")
                    update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)
                elif direction == "SHORT" and current_price >= sl:
                    print(f"[{datetime.now()}] 🛑 Ціна {current_price} пробила SL {sl} для SHORT {symbol} до входу. Скасовуємо ордер {order_id}.")
                    safe_api_call(exchange.cancel_order, order_id, symbol)
                    send_telegram_message(f"🛑 <b>Скасовано ордер (SL пробито до входу)</b>\nМонета: <b>{symbol}</b>\nЦіна: {current_price} | SL: {sl}")
                    update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)


def sync_open_trades(exchange):
    """Синхронізує відкриті угоди в базі даних з їх реальним статусом на Bybit."""
    try:
        open_trades = get_open_trades()
        if not open_trades:
            return

        # Отримуємо відкриті позиції на біржі
        positions = get_open_positions(exchange)
        
        for t in open_trades:
            symbol = t.get("symbol")
            direction = t.get("direction")
            order_id = t.get("order_id")
            entry_price = t.get("entry_price")
            
            if not symbol:
                continue
                
            # 1. Перевіряємо, чи є активна позиція по цьому символу
            symbol_positions = [p for p in positions if p.get("symbol") == symbol]
            side_target = "buy" if direction == "LONG" else "sell"
            has_active_position = False
            
            for p in symbol_positions:
                side = str(p.get("side") or p.get("info", {}).get("side", "")).lower()
                if side in {"buy", "long"} and side_target == "buy":
                    has_active_position = True
                elif side in {"sell", "short"} and side_target == "sell":
                    has_active_position = True
                    
            if has_active_position:
                # Позиція ще відкрита
                continue
                
            # 2. Якщо позиції немає, перевіряємо чи активний ще лімітний ордер на Bybit
            open_orders = get_open_orders(exchange, symbol)
            order_is_active = False
            if order_id:
                order_is_active = any(o.get("id") == order_id for o in open_orders)
            else:
                order_is_active = has_same_direction_open_order(open_orders, direction)
                
            if order_is_active:
                # Ордер ще чекає у стакані
                continue
                
            # 3. Угоду закрили або скасували
            was_filled = False
            actual_pnl = 0.0
            exit_price = entry_price
            
            if order_id:
                try:
                    order_info = exchange.fetch_order(order_id, symbol)
                    status = order_info.get("status")
                    if status == "canceled" or status == "rejected":
                        print(f"[{datetime.now()}] ℹ️ Ордер {order_id} ({symbol}) скасовано.")
                        update_trade_status(symbol=symbol, status="CANCELLED", pnl=0.0, order_id=order_id)
                        continue
                    elif status == "closed":
                        was_filled = True
                except Exception as e:
                    print(f"[{datetime.now()}] ⚠️ Не вдалося отримати статус ордера {order_id}: {e}")
                    # Вважаємо закритим, якщо його немає в активних
                    was_filled = True
            else:
                was_filled = True
                
            if was_filled:
                try:
                    closed_pnl_records = fetch_closed_pnl_bybit(exchange, symbol)
                    if closed_pnl_records:
                        latest_pnl = closed_pnl_records[-1]
                        actual_pnl = float(latest_pnl.get("closedPnl") or 0.0)
                        exit_price = float(latest_pnl.get("avgExitPrice") or entry_price)
                        status_outcome = "WIN" if actual_pnl > 0 else "LOSS"
                        
                        update_trade_status(symbol=symbol, status=status_outcome, pnl=actual_pnl, order_id=order_id)
                        record_trade(symbol, direction, entry_price, exit_price, actual_pnl)
                        print(f"[{datetime.now()}] 🎯 Угода {symbol} закрита: {status_outcome} PnL={actual_pnl:.4f} USDT")
                        continue
                except Exception as e:
                    print(f"[{datetime.now()}] ⚠️ Помилка fetch_closed_pnl_bybit для {symbol}: {e}")
                    
                # Fallback за логікою цінових рівнів
                try:
                    ohlcv = fetch_data(exchange, symbol, "15m", limit=20)
                    if ohlcv is not None:
                        highs = ohlcv["high"].tolist()
                        lows = ohlcv["low"].tolist()
                        tp2 = t.get("take_profit_2") or (entry_price * 1.05 if direction == "LONG" else entry_price * 0.95)
                        sl = t.get("stop_loss") or (entry_price * 0.98 if direction == "LONG" else entry_price * 1.02)
                        
                        reached_tp = False
                        reached_sl = False
                        
                        if direction == "LONG":
                            if max(highs) >= tp2:
                                reached_tp = True
                            if min(lows) <= sl:
                                reached_sl = True
                        else:
                            if min(lows) <= tp2:
                                reached_tp = True
                            if max(highs) >= sl:
                                reached_sl = True
                                
                        if reached_tp and not reached_sl:
                            status_outcome = "WIN"
                            exit_price = tp2
                            actual_pnl = abs(tp2 - entry_price)
                        elif reached_sl:
                            status_outcome = "LOSS"
                            exit_price = sl
                            actual_pnl = -abs(entry_price - sl)
                        else:
                            status_outcome = "WIN"
                            exit_price = entry_price
                            actual_pnl = 0.0
                            
                        update_trade_status(symbol=symbol, status=status_outcome, pnl=actual_pnl, order_id=order_id)
                        record_trade(symbol, direction, entry_price, exit_price, actual_pnl)
                        print(f"[{datetime.now()}] 🎯 Угода {symbol} закрита (fallback): {status_outcome} PnL={actual_pnl:.4f} USDT")
                except Exception as e_fallback:
                    print(f"[{datetime.now()}] ⚠️ Не вдалося синхронізувати угоду {symbol} через fallback: {e_fallback}")
    except Exception as e_sync:
        print(f"[{datetime.now()}] ⚠️ Помилка у sync_open_trades: {e_sync}")


def run_bot():
    # Ініціалізуємо БД
    init_db()
    
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

    send_telegram_message(
        f"🚀 <b>SMC Racer (Мульти-Агентна версія)</b> активована!\n"
        f"Режим: <b>DEMO TRADING</b> (Плече 10x, Ризик 1%)\n"
        f"Доступно пар на Bybit: <b>{len(all_symbols)}</b>.\n"
        f"Очікую сигнали..."
    )
    print(f"[{datetime.now()}] Бот запущений. Доступно {len(all_symbols)} пар на демо рахунку.")

    if require_confirmation and TELEGRAM_BOT_TOKEN:
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
                    sleep_time = min(30.0, 0.8 * (2 ** consecutive_failures))
                    time.sleep(sleep_time)
        threading.Thread(target=_tg_callback_loop, name="tg-callback-poller", daemon=True).start()
        print(f"[{datetime.now()}] ✅ Telegram callback poller запущено в окремому потоці (з адаптивним бекоффом)")

    # Запускаємо перший цикл консенсусу ШІ-агентів через 10 секунд після старту
    last_agents_run = time.time() - 86000 # Запустить через 40 секунд після запуску бота
    dry_cycles_without_setups = 0
    last_health_ping = time.time()
    last_ai_signal_ping = time.time()

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
        session_dd = ((session_start_equity - equity_now) / max(session_start_equity, 1.0)) * 100.0
        daily_dd = ((day_start_equity - equity_now) / max(day_start_equity, 1.0)) * 100.0
        if session_dd >= max_session_drawdown_pct or daily_dd >= max_daily_loss_pct:
            print(f"[{datetime.now()}] 🛑 Risk guard stop: session_dd={session_dd:.2f}% daily_dd={daily_dd:.2f}%")
            time.sleep(60)
            continue
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
                    if can_open_position(symbol, direction, positions, open_orders, CONFIG):
                        print(f"[{datetime.now()}] 📤 Підтверджений сигнал, відправляємо ордер на DEMO: {symbol} {direction}")
                        order = execute_demo_order(
                            exchange=exchange,
                            symbol=symbol,
                            direction=direction,
                            entry=sig["entry"],
                            sl=sig["sl"],
                            tp1=sig["tp1"],
                            tp2=sig["tp2"],
                            risk_pct=CONFIG["risk_pct"],
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
        sync_open_trades(exchange)
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

        for symbol in cycle_symbols:
            # Критично для швидкості: не чекаємо кінця циклу, обробляємо підтвердження одразу між парами.
            process_pending_confirmations()
            try:
                # 1. Завантажуємо 15m і 4h (HTF) дані
                df = fetch_data(exchange, symbol, TIMEFRAME, limit=100)
                htf_df = fetch_data(exchange, symbol, "4h", limit=50)
                if df is None or htf_df is None:
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
                        # Сповіщення в Telegram
                        if require_confirmation and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
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
                        if not can_open_position(symbol, direction, positions, open_orders, CONFIG):
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
                                risk_pct=CONFIG["risk_pct"],
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
        # Heartbeat diagnostics source: остання пара з найменшим запасом до ADX порогу
        diag_pair = None
        diag_margin = 1e9
        diag_block = {}
        for symbol in cycle_symbols:
            try:
                df = fetch_data(exchange, symbol, TIMEFRAME, limit=100)
                htf_df = fetch_data(exchange, symbol, "4h", limit=50)
                if df is None or htf_df is None:
                    continue
                st = analyze_racer(df, htf_df, CONFIG)[-1]
                adx_v = float(st.adx) if not pd.isna(st.adx) else 0.0
                adx_t = float(getattr(st, "adx_threshold", CONFIG.get("adx_min", CONFIG.get("adx_thresh", 15))))
                rel_vol = float(getattr(st, "rel_vol", 0.0))
                vol_t = float(CONFIG.get("vol_multiplier_min", CONFIG.get("vol_mult", 1.0)))
                fvg_sz = float(getattr(st, "fvg_size_atr", 0.0))
                fvg_t = float(CONFIG.get("fvg_min_size", 0.08))
                margin = abs(adx_t - adx_v)
                if margin < diag_margin:
                    diag_margin = margin
                    diag_pair = symbol
                    diag_block = {"adx": adx_v, "adx_t": adx_t, "vol": rel_vol, "vol_t": vol_t, "fvg": fvg_sz, "fvg_t": fvg_t}
            except Exception:
                continue

        if time.time() - last_health_ping > 7200:
            if diag_pair:
                adx_line = f"ADX: {diag_block['adx']:.2f} {'<' if diag_block['adx'] < diag_block['adx_t'] else '>='} поріг {diag_block['adx_t']:.2f}"
                vol_line = f"VOL: {diag_block['vol']:.2f} {'<' if diag_block['vol'] < diag_block['vol_t'] else '>='} поріг {diag_block['vol_t']:.2f}"
                fvg_line = f"FVG: {diag_block['fvg']:.2f} {'<' if diag_block['fvg'] < diag_block['fvg_t'] else '>='} поріг {diag_block['fvg_t']:.2f}"
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

        # Перевірка для чергового запуску ШІ-агентів (раз на 24 години)
        if time.time() - last_agents_run > 86400:
            try:
                from cooperative_agents import run_cooperative_agent_consensus
                run_cooperative_agent_consensus(exchange, symbol_window, TIMEFRAME, CONFIG_PATH)
                last_agents_run = time.time()
            except Exception as ae:
                print(f"Помилка запуску консенсусу агентів: {ae}")

        # Пауза 60 сек між повними колами сканування ринку
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
