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
    "adx_thresh": 20,
    "vol_mult": 1.5,
    "fvg_min_size": 0.5,
}

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TIMEFRAME = "15m"

# Зберігаємо стан для кожної пари (остання свічка, де знайдено сетап)
last_setup_bars = {sym: None for sym in SYMBOLS}

def init_bybit():
    """Ініціалізація Bybit Testnet через ccxt."""
    exchange = ccxt.bybit({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': True, # Авто-синхронізація часу з біржею
            'recvWindow': 10000 # Збільшуємо вікно затримки до 10 секунд
        }
    })
    exchange.set_sandbox_mode(True) # Вмикаємо Testnet!
    return exchange

def fetch_data(exchange, symbol, timeframe, limit=100):
    """Отримує OHLCV дані з біржі."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df

def run_bot():
    exchange = init_bybit()
    send_telegram_message("🚀 <b>SMC Racer</b> успішно запущено на Bybit Testnet!\nОчікую торгові сетапи...")
    print(f"[{datetime.now()}] Бот запущений. Торгуємо: {SYMBOLS}")

    while True:
        try:
            for symbol in SYMBOLS:
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

                        # ТУТ МАЄ БУТИ КОД exchange.create_order(...)
                        # Наразі ми просто логуємо, щоб не ризикувати реальними лімітами
                        # доки ти не перевіриш все очима.

            # Чекаємо перед наступним скануванням (наприклад, 1 хвилину)
            time.sleep(60)

        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ Помилка: {e}")
            time.sleep(30) # Чекаємо і пробуємо знову

if __name__ == "__main__":
    run_bot()
