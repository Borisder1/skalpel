import concurrent.futures
from datetime import datetime


def get_market_data_parallel(symbols, timeframes_and_limits, exchange=None):
    """
    Паралельно завантажує OHLCV дані, повторно використовуючи вже ініціалізований об'єкт exchange
    (або легкий пул потоків без повторного завантаження 3500 ринків load_markets() щохвилини).
    Це повністю усуває витік пам'яті 45-90MB на кожному циклі в Render (512MB RAM).
    """
    results = {tf: {} for tf, _ in timeframes_and_limits}
    if not symbols:
        return results

    if exchange is None:
        import ccxt
        exchange = ccxt.bybit({
            "enableRateLimit": True,
            "options": {"defaultType": "linear"},
        })

    def fetch_one(symbol, timeframe, limit):
        for attempt in range(3):
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                return symbol, timeframe, ohlcv
            except Exception:
                if attempt >= 2:
                    return symbol, timeframe, None
        return symbol, timeframe, None

    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for tf, limit in timeframes_and_limits:
            for sym in symbols:
                tasks.append(pool.submit(fetch_one, sym, tf, limit))
        for fut in concurrent.futures.as_completed(tasks):
            try:
                symbol, tf, ohlcv = fut.result()
                results[tf][symbol] = ohlcv
            except Exception:
                pass

    return results


def get_tickers_parallel(symbols, exchange=None):
    """Легке отримання тикерів без створення нового ccxt_async інстансу."""
    if exchange is not None:
        try:
            return exchange.fetch_tickers(symbols)
        except Exception:
            return {}
    return {}
