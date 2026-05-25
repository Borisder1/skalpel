import os
import time
import json
import ccxt
import numpy as np
from ccxt.base.errors import RateLimitExceeded, BadSymbol
from json import JSONDecodeError
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

from racer_core import analyze_racer
from telegram_notifier import send_telegram_message
from db_logger import init_db, log_trade, update_trade_status
from ai_signal_agent import generate_ai_signal
from adaptive_filters import AdaptiveFilterManager

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'active_config.json')
TIMEFRAME = "15m"
MIN_CANDLES_REQUIRED = 50
DEBUG_PAIRS = {"BEAT/USDT:USDT", "BILL/USDT:USDT"}

# Зберігаємо стан для кожної пари (остання свічка, де знайдено сетап)
last_setup_bars = {}
last_order_times = {}
# FIXED: глобальні обмеження ризику депозиту.
MAX_DAILY_LOSS_PCT = 3.0
MAX_SESSION_DRAWDOWN_PCT = 8.0
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
        "risk_pct": 1.0,
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

def execute_demo_order(exchange, symbol, direction, entry, sl, tp1, tp2, risk_pct):
    """Виставляє реальний лімітний ордер на Bybit Demo з Stop Loss та Take Profit."""
    try:
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

def run_bot():
    # Ініціалізуємо БД
    init_db()
    
    startup_config = load_dynamic_config()
    exchange = init_bybit(startup_config)
    
    # Завантажуємо повний список доступних символів один раз
    all_symbols = get_all_usdt_symbols(exchange)
    cycle_index = 0
    filter_manager = AdaptiveFilterManager()
    session_start_equity = None
    day_start_equity = None
    day_marker = None

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
        free_now = float((bal.get("USDT") or {}).get("free", 0.0) or 0.0) or 10000.0
        if session_start_equity is None:
            session_start_equity = free_now
        today = datetime.now(timezone.utc).date()
        if day_marker != today:
            day_marker = today
            day_start_equity = free_now
        session_dd = ((session_start_equity - free_now) / max(session_start_equity, 1.0)) * 100.0
        daily_dd = ((day_start_equity - free_now) / max(day_start_equity, 1.0)) * 100.0
        if session_dd >= MAX_SESSION_DRAWDOWN_PCT or daily_dd >= MAX_DAILY_LOSS_PCT:
            print(f"[{datetime.now()}] 🛑 Risk guard stop: session_dd={session_dd:.2f}% daily_dd={daily_dd:.2f}%")
            time.sleep(60)
            continue
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
                    print(f"[{datetime.now()}]   BOS bull: N/A (racer_core не рахує BOS)")
                    print(f"[{datetime.now()}]   BOS bear: N/A (racer_core не рахує BOS)")
                    print(f"[{datetime.now()}]   CHoCH: N/A (racer_core не рахує CHoCH)")
                    print(f"[{datetime.now()}]   OB active: N/A (racer_core не веде OB state)")
                    print(f"[{datetime.now()}]   FVG naked bull/bear: {getattr(last_state, 'bull_fvg', False)}/{getattr(last_state, 'bear_fvg', False)}")
                    print(f"[{datetime.now()}]   Confluence: N/A (в racer_core немає score)")
                    print(f"[{datetime.now()}]   HTF trend bull/bear: {last_state.is_htf_bullish}/{last_state.is_htf_bearish}")
                    print(f"[{datetime.now()}]   Session: N/A (в racer_core немає session фільтра)")
                    print(f"[{datetime.now()}]   Impulse bull/bear: {getattr(last_state, 'is_impulse_bull', False)}/{getattr(last_state, 'is_impulse_bear', False)}")
                    print(f"[{datetime.now()}]   Final decision (setup_found): {setup_found}")

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
                        if CONFIG.get("dry_run", True):
                            print(f"[{datetime.now()}] 🧪 dry_run=true, ордер НЕ відправлено для {symbol}")
                        else:
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
