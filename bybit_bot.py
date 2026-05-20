import os
import time
import json
import ccxt
from ccxt.base.errors import RateLimitExceeded, BadSymbol
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

from racer_core import analyze_racer
from telegram_notifier import send_telegram_message
from db_logger import init_db, log_trade, update_trade_status

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'active_config.json')
TIMEFRAME = "15m"

# Зберігаємо стан для кожної пари (остання свічка, де знайдено сетап)
last_setup_bars = {}


def build_runtime_config(base_config: dict, dry_cycles_without_setups: int) -> dict:
    """Поступово послаблює фільтри, якщо ринок довго без сигналів."""
    cfg = dict(base_config)

    if dry_cycles_without_setups >= 3:
        cfg["vol_mult"] = max(0.85, float(cfg.get("vol_mult", 1.1)) - 0.1)
    if dry_cycles_without_setups >= 5:
        cfg["fvg_min_size"] = max(0.05, float(cfg.get("fvg_min_size", 0.2)) - 0.05)
    if dry_cycles_without_setups >= 7:
        cfg["adx_thresh"] = max(8, int(cfg.get("adx_thresh", 15)) - 2)

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
        except Exception as e:
            print(f"Помилка завантаження active_config.json: {e}")
    
    # Дефолтні налаштування
    return {
        "fib_level": 0.5,
        "sl_atr_mult": 1.0,
        "tp1_rr": 1.0,
        "tp2_rr": 2.5,
        "risk_pct": 1.0,
        "liq_lookback": 20,
        "adx_thresh": 15,
        "vol_mult": 1.1,
        "fvg_min_size": 0.2,
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

def init_bybit():
    """Ініціалізація Bybit для демо-торгівлі за допомогою API-ключів."""
    exchange_params = {
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
            'recvWindow': 10000
        }
    }
    
    if API_KEY and API_SECRET:
        exchange_params['apiKey'] = API_KEY
        exchange_params['secret'] = API_SECRET
        print(f"[{datetime.now()}] API-ключі виявлені. Авторизуємось на Bybit.")
    else:
        print(f"[{datetime.now()}] API-ключі відсутні. Використовуємо публічний доступ.")

    exchange = ccxt.bybit(exchange_params)
    
    # Активуємо режим Demo Trading для безпеки
    try:
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
    return df

def execute_demo_order(exchange, symbol, direction, entry, sl, tp1, tp2, risk_pct):
    """Виставляє реальний лімітний ордер на Bybit Demo з Stop Loss та Take Profit."""
    try:
        # 1. Отримуємо демо-баланс
        balance_info = exchange.fetch_balance()
        free_usdt = balance_info.get('USDT', {}).get('free', 0.0)
        
        # Якщо баланс на демо рахунку нульовий або не отриманий, використовуємо умовний ліміт для тестів
        if free_usdt <= 0:
            free_usdt = 10000.0 # Дефолтний умовний демо-баланс

        # 2. Розраховуємо ризик і об'єм
        price_diff = abs(entry - sl)
        if price_diff <= 0:
            return None
            
        risk_amount = free_usdt * (risk_pct / 100.0)
        qty = risk_amount / price_diff
        
        # Отримуємо специфікації монети та форматуємо ціну/кількість до точності біржі
        price_str = exchange.price_to_precision(symbol, entry)
        qty_str = exchange.amount_to_precision(symbol, qty)
        
        if float(qty_str) <= 0:
            # Захист від занадто малих позицій на дешевих монетах
            qty_str = exchange.amount_to_precision(symbol, qty * 10)
            
        side = 'buy' if direction == "LONG" else 'sell'
        
        # Налаштовуємо параметри TP/SL для Bybit V5
        params = {
            'takeProfit': exchange.price_to_precision(symbol, tp2),
            'stopLoss': exchange.price_to_precision(symbol, sl),
            'tpslMode': 'Full'
        }
        
        # Встановлюємо кредитне плече 10x перед угодою
        try:
            exchange.set_leverage(10, symbol)
        except Exception:
            pass # Плече вже встановлене
            
        print(f"[{datetime.now()}] 🛒 ДЕМО-ОРДЕР: {side.upper()} {qty_str} {symbol} по {price_str} (SL: {sl:.4f}, TP: {tp2:.4f})")
        
        order = exchange.create_order(
            symbol=symbol,
            type='limit',
            side=side,
            amount=float(qty_str),
            price=float(price_str),
            params=params
        )
        
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

def run_bot():
    # Ініціалізуємо БД
    init_db()
    
    exchange = init_bybit()
    
    # Завантажуємо повний список доступних символів один раз
    all_symbols = get_all_usdt_symbols(exchange)
    cycle_index = 0

    send_telegram_message(
        f"🚀 <b>SMC Racer (Мульти-Агентна версія)</b> активована!\n"
        f"Режим: <b>DEMO TRADING</b> (Плече 10x, Ризик 1%)\n"
        f"Доступно пар на Bybit: <b>{len(all_symbols)}</b>.\n"
        f"Очікую сигнали..."
    )
    print(f"[{datetime.now()}] Бот запущений. Доступно {len(all_symbols)} пар на демо рахунку.")

    # Запускаємо перший цикл консенсусу ШІ-агентів через 10 секунд після старту
    last_agents_run = time.time() - 86000 # Запустить через 40 секунд після запуску бота
    dry_cycles_without_setups = 0
    last_health_ping = time.time()

    while True:
        # Динамічно завантажуємо конфігурацію на початку кожного сканування
        base_config = load_dynamic_config()
        CONFIG = build_runtime_config(base_config, dry_cycles_without_setups)
        cycle_scanned = 0
        cycle_setups = 0
        cycle_invalid_symbols = 0
        cycle_rate_limits = 0

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
            try:
                # 1. Завантажуємо 15m і 4h (HTF) дані
                df = fetch_data(exchange, symbol, TIMEFRAME, limit=100)
                htf_df = fetch_data(exchange, symbol, "4h", limit=50)
                cycle_scanned += 1

                # 2. Проганяємо логіку Racer
                states = analyze_racer(df, htf_df, CONFIG)
                last_state = states[-1]

                # 3. Перевіряємо сетап
                if last_state.setup and last_state.setup.valid:
                    if last_setup_bars[symbol] != last_state.timestamp:
                        last_setup_bars[symbol] = last_state.timestamp
                        
                        cycle_setups += 1
                        setup = last_state.setup
                        direction_str = "LONG 🟢" if setup.dir == 1 else "SHORT 🔴"
                        
                        msg = (
                            f"🔔 <b>СИГНАЛ {direction_str}</b> | {symbol}\n"
                            f"Вхід (Limit): <b>{setup.entry:.4f}</b>\n"
                            f"Stop Loss: <b>{setup.sl:.4f}</b>\n"
                            f"Take Profit 1: <b>{setup.tp1:.4f}</b>\n"
                            f"Take Profit 2: <b>{setup.tp2:.4f}</b>"
                        )
                        print(f"[{datetime.now()}] {msg.replace('<b>', '').replace('</b>', '')}")
                        
                        # Сповіщення в Telegram
                        send_telegram_message(msg)
                        
                        # Логування в SQLite
                        log_trade(
                            symbol=symbol,
                            direction="LONG" if setup.dir == 1 else "SHORT",
                            entry=setup.entry,
                            sl=setup.sl,
                            tp1=setup.tp1,
                            tp2=setup.tp2,
                            fib=CONFIG["fib_level"],
                            sl_mult=CONFIG["sl_atr_mult"]
                        )
                        
                        # Вхід на Демо рахунку Bybit
                        execute_demo_order(
                            exchange=exchange,
                            symbol=symbol,
                            direction="LONG" if setup.dir == 1 else "SHORT",
                            entry=setup.entry,
                            sl=setup.sl,
                            tp1=setup.tp1,
                            tp2=setup.tp2,
                            risk_pct=CONFIG["risk_pct"]
                        )
                
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
                time.sleep(1)
                continue


        if cycle_setups == 0:
            dry_cycles_without_setups += 1
        else:
            dry_cycles_without_setups = 0

        print(
            f"[{datetime.now()}] 📊 Цикл завершено | scanned={cycle_scanned} setups={cycle_setups} "
            f"invalid={cycle_invalid_symbols} ratelimit={cycle_rate_limits} dry_cycles={dry_cycles_without_setups} "
            f"cfg(adx={CONFIG.get('adx_thresh')}, vol={CONFIG.get('vol_mult')}, fvg={CONFIG.get('fvg_min_size')})"
        )

        if time.time() - last_health_ping > 7200:
            send_telegram_message(
                f"🩺 <b>Heartbeat SMC Racer</b>\n"
                f"Скановано пар: <b>{cycle_scanned}</b>\n"
                f"Сетапів за цикл: <b>{cycle_setups}</b>\n"
                f"RateLimit помилок: <b>{cycle_rate_limits}</b>\n"
                f"Invalid symbols: <b>{cycle_invalid_symbols}</b>\n"
                f"Dry циклів поспіль: <b>{dry_cycles_without_setups}</b>\n"
                f"Фільтри зараз → ADX: <b>{CONFIG.get('adx_thresh')}</b>, VOL: <b>{CONFIG.get('vol_mult')}</b>, FVG: <b>{CONFIG.get('fvg_min_size')}</b>"
            )
            last_health_ping = time.time()

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
