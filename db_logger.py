import logging
import os
import sqlite3
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history.db")
logger = logging.getLogger(__name__)

def get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    """Створює таблицю, якщо її не існує."""
    with get_db_conn() as conn:
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
                pnl REAL,
                order_id TEXT,
                quant_score REAL,
                factors_snapshot TEXT
            )
            """
        )
        # Додаємо нові колонки, якщо БД вже створена
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN order_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN quant_score REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN factors_snapshot TEXT")
        except sqlite3.OperationalError:
            pass
    logger.info("База даних ініціалізована: %s", DB_PATH)


def log_trade(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float, fib: float, sl_mult: float, order_id: str = None, quant_score: float = None, factors_snapshot: dict = None):
    """Записує нову угоду в БД."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    factors_json = json.dumps(factors_snapshot) if factors_snapshot else None
    initial_status = "VIRTUAL_OPEN" if order_id and order_id.startswith("VIRTUAL_") else "OPEN"
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO trades (
                timestamp, symbol, direction, entry_price, stop_loss,
                take_profit_1, take_profit_2, fib_level, sl_atr_mult, status, pnl, order_id, quant_score, factors_snapshot
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?)
            """,
            (timestamp, symbol, direction, entry, sl, tp1, tp2, fib, sl_mult, initial_status, order_id, quant_score, factors_json),
        )


def get_open_trades():
    """Повертає всі відкриті (реальні та віртуальні) угоди з бази даних."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE status IN ('OPEN', 'VIRTUAL_OPEN')")
        return [dict(row) for row in cursor.fetchall()]


def update_trade_status(symbol: str, status: str, pnl: float, order_id: str = None):
    """Оновлює статус угоди по order_id або символу."""
    with get_db_conn() as conn:
        if order_id:
            conn.execute(
                """
                UPDATE trades
                SET status = ?, pnl = ?
                WHERE order_id = ? AND status IN ('OPEN', 'VIRTUAL_OPEN')
                """,
                (status, pnl, order_id),
            )
        else:
            conn.execute(
                """
                UPDATE trades
                SET status = ?, pnl = ?
                WHERE id = (
                    SELECT id FROM trades
                    WHERE symbol = ? AND status IN ('OPEN', 'VIRTUAL_OPEN')
                    ORDER BY id DESC
                    LIMIT 1
                )
                """,
                (status, pnl, symbol),
            )


def get_trade_by_order_id(order_id: str):
    """Повертає інформацію про угоду за її order_id."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
