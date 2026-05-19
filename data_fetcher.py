"""
SMC Agent v6 — Data Fetcher
Завантаження OHLCV з Binance через ccxt з кешуванням у CSV.
"""
import os
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def cache_path(symbol: str, timeframe: str) -> str:
    safe_sym = symbol.replace("/", "_").replace(":", "_")
    return os.path.join(DATA_DIR, f"{safe_sym}_{timeframe}.csv")


def fetch_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "15m",
    days: int = 180,
    exchange_id: str = "binance",
) -> pd.DataFrame:
    """
    Завантажує OHLCV дані з біржі або кешу.
    
    Args:
        symbol: Торгова пара (BTC/USDT, ETH/USDT, DOGE/USDT)
        timeframe: Таймфрейм (1m, 5m, 15m, 1h, 4h, 1d)
        days: Кількість днів історії
        exchange_id: Біржа (binance за замовчуванням)
    
    Returns:
        DataFrame з колонками: timestamp, open, high, low, close, volume
    """
    ensure_data_dir()
    cp = cache_path(symbol, timeframe)

    # Спробуємо завантажити з кешу
    if os.path.exists(cp):
        df = pd.read_csv(cp, parse_dates=["timestamp"])
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        df = df[df["timestamp"] >= cutoff].reset_index(drop=True)
        age_hours = (datetime.now(timezone.utc) - df["timestamp"].max().to_pydatetime().replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if len(df) > 100 and age_hours < 12:
            print(f"  ✅ Cache hit: {cp} ({len(df)} bars, age {age_hours:.1f}h)")
            return df

    # Завантаження з біржі
    try:
        import ccxt
    except ImportError:
        raise ImportError("ccxt не встановлено. Запустіть: pip install ccxt")

    print(f"  ⬇️  Fetching {symbol} {timeframe} ({days} days) from {exchange_id}...")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})

    tf_seconds = _tf_to_seconds(timeframe)
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)

    all_ohlcv = []
    current_since = since
    batch_limit = 1000

    while current_since < end:
        try:
            ohlcv = exchange.fetch_ohlcv(
                symbol, timeframe, since=current_since, limit=batch_limit
            )
        except Exception as e:
            print(f"  ⚠️  Error fetching {symbol}: {e}")
            break

        if not ohlcv:
            break

        all_ohlcv.extend(ohlcv)
        current_since = ohlcv[-1][0] + tf_seconds * 1000

        # Rate limit
        time.sleep(exchange.rateLimit / 1000)

        if len(all_ohlcv) % 5000 == 0:
            print(f"    ... {len(all_ohlcv)} bars loaded")

    if not all_ohlcv:
        raise ValueError(f"Не вдалося завантажити дані для {symbol} {timeframe}")

    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    # Зберегти в кеш
    df.to_csv(cp, index=False)
    print(f"  ✅ Saved {len(df)} bars to {cp}")

    return df


def fetch_htf_ohlcv(
    symbol: str = "BTC/USDT",
    htf: str = "4h",
    days: int = 365,
    exchange_id: str = "binance",
) -> pd.DataFrame:
    """Завантаження HTF даних для тренду."""
    return fetch_ohlcv(symbol, htf, days, exchange_id)


def resample_to_htf(df: pd.DataFrame, htf: str = "4h") -> pd.DataFrame:
    """Ресемплювання з нижчого TF у HTF (альтернатива окремому завантаженню)."""
    rule_map = {
        "1h": "1h", "4h": "4h", "1d": "1D",
        "2h": "2h", "8h": "8h", "12h": "12h",
    }
    rule = rule_map.get(htf, "4h")
    
    resampled = df.set_index("timestamp").resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna().reset_index()
    
    return resampled


def load_csv(filepath: str) -> pd.DataFrame:
    """Завантаження з локального CSV (альтернатива ccxt)."""
    df = pd.read_csv(filepath)
    
    # Автоматичне визначення колонок
    col_map = {}
    for col in df.columns:
        cl = col.lower().strip()
        if cl in ("timestamp", "date", "datetime", "time"):
            col_map[col] = "timestamp"
        elif cl in ("open", "o"):
            col_map[col] = "open"
        elif cl in ("high", "h"):
            col_map[col] = "high"
        elif cl in ("low", "l"):
            col_map[col] = "low"
        elif cl in ("close", "c"):
            col_map[col] = "close"
        elif cl in ("volume", "vol", "v"):
            col_map[col] = "volume"
    
    df = df.rename(columns=col_map)
    
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    for r in required:
        if r not in df.columns:
            raise ValueError(f"Колонка '{r}' не знайдена. Наявні: {list(df.columns)}")
    
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df[required].sort_values("timestamp").reset_index(drop=True)
    
    return df


def _tf_to_seconds(tf: str) -> int:
    """Конвертація таймфрейму в секунди."""
    unit = tf[-1]
    val = int(tf[:-1])
    multiplier = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    return val * multiplier.get(unit, 60)


# ===== Допоміжні функції для мульти-символьного тесту =====

SYMBOLS = ["BTC/USDT", "ETH/USDT", "DOGE/USDT"]

def fetch_all_symbols(
    symbols: list = None,
    timeframe: str = "15m",
    days: int = 180,
    exchange_id: str = "binance",
) -> dict:
    """
    Завантажує дані для кількох символів.
    
    Returns:
        dict: {symbol: DataFrame}
    """
    if symbols is None:
        symbols = SYMBOLS
    
    result = {}
    for sym in symbols:
        print(f"\n📊 {sym}:")
        try:
            result[sym] = fetch_ohlcv(sym, timeframe, days, exchange_id)
        except Exception as e:
            print(f"  ❌ Failed: {e}")
    
    return result


if __name__ == "__main__":
    # Тест завантаження
    data = fetch_all_symbols(timeframe="15m", days=7)
    for sym, df in data.items():
        print(f"\n{sym}: {len(df)} bars, {df['timestamp'].min()} → {df['timestamp'].max()}")
        print(df.tail(3).to_string(index=False))
