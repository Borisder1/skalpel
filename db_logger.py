import logging
import os
import sqlite3
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history.db")
logger = logging.getLogger(__name__)

# V10: Blacklist deduplication cache
_blacklist_dedup = {}  # {symbol: last_blacklist_timestamp}

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
                factors_snapshot TEXT,
                breakeven_activated INTEGER DEFAULT 0
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
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN breakeven_activated INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        # V8.5: AI Memory
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                event_type TEXT,
                best_weights_json TEXT,
                market_regime TEXT,
                simulated_pnl REAL,
                report TEXT
            )
            """
        )
        # V9.0: Symbol Blacklist
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS symbol_blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT UNIQUE,
                reason TEXT,
                blacklisted_at TEXT,
                expires_at TEXT,
                loss_count INTEGER DEFAULT 0
            )
            """
        )
    logger.info("База даних ініціалізована: %s", DB_PATH)


def log_trade(symbol: str, direction: str, entry: float, sl: float, tp1: float, tp2: float, fib: float, sl_mult: float, order_id: str = None, quant_score: float = None, factors_snapshot: dict = None):
    """Записує нову угоду в БД."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    factors_json = json.dumps(factors_snapshot) if factors_snapshot else None
    initial_status = "VIRTUAL_OPEN" if order_id and order_id.startswith("VIRTUAL_") else "OPEN"
    # V11: Конвертуємо numpy.float32 в Python float (інакше SQLite зберігає як BLOB)
    entry = float(entry) if entry is not None else 0.0
    sl = float(sl) if sl is not None else 0.0
    tp1 = float(tp1) if tp1 is not None else 0.0
    tp2 = float(tp2) if tp2 is not None else 0.0
    fib = float(fib) if fib is not None else 0.0
    sl_mult = float(sl_mult) if sl_mult is not None else 0.0
    quant_score = float(quant_score) if quant_score is not None else None
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
def update_breakeven_status(symbol: str, order_id: str = None, status: int = 1):
    """Оновлює прапорець активації безубитку для угоди."""
    with get_db_conn() as conn:
        if order_id:
            conn.execute(
                "UPDATE trades SET breakeven_activated = ? WHERE order_id = ?",
                (status, order_id)
            )
        else:
            conn.execute(
                """
                UPDATE trades SET breakeven_activated = ? 
                WHERE id = (
                    SELECT id FROM trades 
                    WHERE symbol = ? AND status IN ('OPEN', 'VIRTUAL_OPEN')
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (status, symbol)
            )


def get_trade_by_order_id(order_id: str):
    """Повертає інформацію про угоду за її order_id."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE order_id = ?", (order_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

# V8.5 AI Memory Functions
def save_ai_memory(event_type: str, best_weights: dict, market_regime: str, simulated_pnl: float, report: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    weights_json = json.dumps(best_weights)
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO ai_memory (timestamp, event_type, best_weights_json, market_regime, simulated_pnl, report)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (timestamp, event_type, weights_json, market_regime, simulated_pnl, report)
        )

def get_latest_ai_memory():
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ai_memory ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if row:
            res = dict(row)
            try:
                res["best_weights"] = json.loads(res["best_weights_json"])
            except:
                res["best_weights"] = {}
            return res
        return None


# V9.1: Progressive Symbol Blacklist Functions
# Прогресивний бан: 4г → 12г → 24г → 48г залежно від кількості збитків

PROGRESSIVE_BAN_HOURS = [4, 12, 24, 48]  # Ескалація блокування


def blacklist_symbol(symbol: str, reason: str, hours: int = None):
    """Додає монету в чорний список з прогресивним збільшенням тривалості.
    
    Якщо hours=None — автоматично визначає тривалість за кількістю попередніх банів:
    1-й бан: 4 години, 2-й: 12 годин, 3-й: 24 години, 4+: 48 годин.
    """
    # V10: Дедуплікація — ігноруємо повторні бани протягом 60 секунд
    import time as _time
    now_ts = _time.time()
    if symbol in _blacklist_dedup and (now_ts - _blacklist_dedup[symbol]) < 60:
        logger.info("🔄 Blacklist dedup: %s вже додано < 60с тому — ігноруємо", symbol)
        return {"hours": 0, "level": 0, "expires_at": "", "deduplicated": True}
    _blacklist_dedup[symbol] = now_ts

    from datetime import timedelta
    now = datetime.now()
    
    # Визначаємо поточний рівень бану для цього символу
    current_loss_count = 0
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT loss_count FROM symbol_blacklist WHERE symbol = ?", (symbol,))
        row = cursor.fetchone()
        if row:
            current_loss_count = row[0]
    
    if hours is None:
        # Прогресивна ескалація
        ban_index = min(current_loss_count, len(PROGRESSIVE_BAN_HOURS) - 1)
        hours = PROGRESSIVE_BAN_HOURS[ban_index]
    
    expires = now + timedelta(hours=hours)
    blacklisted_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = expires.strftime("%Y-%m-%d %H:%M:%S")
    
    with get_db_conn() as conn:
        conn.execute(
            """
            INSERT INTO symbol_blacklist (symbol, reason, blacklisted_at, expires_at, loss_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(symbol) DO UPDATE SET
                reason = excluded.reason,
                blacklisted_at = excluded.blacklisted_at,
                expires_at = excluded.expires_at,
                loss_count = loss_count + 1
            """,
            (symbol, reason, blacklisted_at, expires_at)
        )
    logger.info("🚫 Blacklisted %s for %dh (level %d) until %s: %s", 
                symbol, hours, current_loss_count + 1, expires_at, reason)
    return {"hours": hours, "level": current_loss_count + 1, "expires_at": expires_at}


def is_blacklisted(symbol: str) -> bool:
    """Перевіряє, чи монета в чорному списку (з урахуванням expires_at)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM symbol_blacklist WHERE symbol = ? AND expires_at > ?",
            (symbol, now_str)
        )
        count = cursor.fetchone()[0]
    return count > 0


def get_blacklist_info(symbol: str) -> dict:
    """Повертає інформацію про бан символу (або None якщо не забанений)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM symbol_blacklist WHERE symbol = ? AND expires_at > ?",
            (symbol, now_str)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_symbol_loss_count(symbol: str, hours: int = 24) -> int:
    """Повертає кількість збиткових угод за останні N годин для символу."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM trades
            WHERE symbol = ? AND status IN ('LOSS', 'VIRTUAL_LOSS') AND timestamp > ?
            """,
            (symbol, cutoff)
        )
        return cursor.fetchone()[0]


def cleanup_expired_blacklist():
    """Видаляє прострочені записи з чорного списку."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM symbol_blacklist WHERE expires_at <= ?", (now_str,))
        deleted = cursor.rowcount
    if deleted > 0:
        logger.info("🧹 Cleaned up %d expired blacklist entries", deleted)
    return deleted


def get_active_blacklist() -> list:
    """Повертає список всіх активних банів."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM symbol_blacklist WHERE expires_at > ? ORDER BY loss_count DESC",
            (now_str,)
        )
        return [dict(row) for row in cursor.fetchall()]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()

