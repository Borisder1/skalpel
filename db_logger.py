import logging
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history.db")
logger = logging.getLogger(__name__)


def init_db():
    """Створює таблицю, якщо її не існує."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                direction TEXT,
                entry_price REAL,
                stop_loss REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                fib_level REAL,
                sl_atr_mult REAL,
                status TEXT,
                pnl REAL
            )
            """
        )
    logger.info("База даних ініціалізована: %s", DB_PATH)


def log_trade(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float, fib: float, sl_mult: float):
    """Записує нову угоду в БД."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO trades (
                timestamp, symbol, direction, entry_price, stop_loss,
                take_profit_1, take_profit_2, fib_level, sl_atr_mult, status, pnl
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 0.0)
            """,
            (timestamp, symbol, direction, entry, sl, tp1, tp2, fib, sl_mult),
        )


def update_trade_status(symbol: str, status: str, pnl: float):
    """Оновлює статус (WIN/LOSS) останньої відкритої угоди по символу."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE trades
            SET status = ?, pnl = ?
            WHERE id = (
                SELECT id FROM trades
                WHERE symbol = ? AND status = 'OPEN'
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (status, pnl, symbol),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
