import os
import time
import ccxt
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

from racer_core import analyze_racer
from telegram_notifier import send_telegram_message
from db_logger import log_trade, update_trade_status

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# Налаштування стратегії (беремо найкращі з бектесту)
CONFIG = {
    "fib_level": 0.5,
    "sl_atr_mult": 1.0,
    "tp1_rr": 1.0,
    "tp2_rr": 2.5,
    "risk_pct": 1.0,  # 1% від балансу
    "liq_lookback": 20,
    "adx_thresh": 15,    # ЗНИЖЕНО (було 20) - тепер бот торгуватиме навіть при слабшому тренді
    "vol_mult": 1.1,     # ЗНИЖЕНО (було 1.5) - менш жорсткі вимоги до об'єму
    "fvg_min_size": 0.2, # ЗНИЖЕНО (було 0.5) - допускаються менші імпульси
}

TIMEFRAME = "15m"

def get_all_usdt_symbols(exchange):
    """Отримує всі активні USDT ф'ючерси на Bybit."""
    print(f"[{datetime.now()}] Завантаження списку всіх монет з Bybit...")
    exchange.load_markets()
    symbols = []
    for symbol, market in exchange.markets.items():
        if market.get('linear') and market.get('quote') == 'USDT' and market.get('active'):
            symbols.append(symbol)
    
    print(f"[{datetime.now()}] Знайдено {len(symbols)} USDT пар для сканування!")
    return symbols

# Зберігаємо стан для кожної пари (остання свічка, де знайдено сетап)
last_setup_bars = {}

def init_bybit():
    """Ініціалізація Bybit для отримання публічних даних (без ключів)."""
    exchange = ccxt.bybit({
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
            'recvWindow': 10000
        }
    })
    
    # Ми не використовуємо sandbox_mode, оскільки просто читаємо публічні графіки
    # з основної біржі Bybit.
    
    # Явно синхронізуємо час ПЕРЕД завантаженням ринків
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

def run_bot():
    exchange = init_bybit()
    
    # Завантажуємо ВСІ доступні криптовалюти
    SYMBOLS = get_all_usdt_symbols(exchange)
    
    # Ініціалізуємо пам'ять для кожної знайденої монети
    global last_setup_bars
    for sym in SYMBOLS:
        last_setup_bars[sym] = None

    send_telegram_message(f"🚀 <b>SMC Racer</b> успішно запущено!\nСканую <b>{len(SYMBOLS)}</b> криптовалют на Bybit.\nОчікую торгові сетапи...")
    print(f"[{datetime.now()}] Бот запущений. Торгуємо ВСІМА доступними парами ({len(SYMBOLS)} шт.)")

    while True:
        for symbol in SYMBOLS:
            try:
                # 1. Завантажуємо 15m і 4h (HTF) дані
                df = fetch_data(exchange, symbol, TIMEFRAME, limit=100)
                htf_df = fetch_data(exchange, symbol, "4h", limit=50)

                # 2. Проганяємо логіку Racer
                states = analyze_racer(df, htf_df, CONFIG)
                last_state = states[-1]

                # 3. Перевіряємо чи є валідний сетап
                if last_state.setup and last_state.setup.valid:
                    # Щоб не спамити один і той самий сетап на кожному тіку
                    if last_setup_bars[symbol] != last_state.timestamp:
                        last_setup_bars[symbol] = last_state.timestamp
                        
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
                        
                        # Відправка в ТГ
                        send_telegram_message(msg)
                        
                        # Логування в БД
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
                
                # Невелика пауза між кожною монетою, щоб Bybit не заблокував (Rate Limit)
                time.sleep(0.5)

            except Exception as e:
                # Якщо якась дивна/нова монета видає помилку, ми просто ігноруємо її і йдемо далі
                if "Symbol Is Invalid" not in str(e):
                    print(f"[{datetime.now()}] ⚠️ Помилка на {symbol}: {e}")
                time.sleep(1)
                continue

        # Чекаємо перед наступним скануванням всього ринку
        time.sleep(60)

if __name__ == "__main__":
    run_bot()
