import asyncio
import ccxt.async_support as ccxt_async

async def fetch_all_data(symbols, timeframe, limit=150):
    """
    Асинхронно завантажує OHLCV дані для всіх символів паралельно.
    Використовує ccxt.async_support та семафор для уникнення Rate Limit.
    """
    exchange = ccxt_async.bybit({'options': {'defaultType': 'linear'}})
    sem = asyncio.Semaphore(15)  # 15 одночасних запитів
    
    async def fetch(symbol):
        async with sem:
            for attempt in range(3):
                try:
                    ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                    return symbol, ohlcv
                except ccxt_async.RateLimitExceeded:
                    await asyncio.sleep(1.0 * (attempt + 1))
                except Exception as e:
                    await asyncio.sleep(1.0)
            return symbol, None

    tasks = [fetch(s) for s in symbols]
    results = await asyncio.gather(*tasks)
    await exchange.close()
    
    return dict(results)

def get_market_data_parallel(symbols, timeframes_and_limits):
    """
    Синхронна обгортка для зручного виклику з головного бота.
    timeframes_and_limits = [("15m", 150), ("4h", 50)]
    Повертає: { "15m": {"BTC/USDT": [...], ...}, "4h": {...} }
    """
    async def run_all():
        results = {}
        for tf, limit in timeframes_and_limits:
            res = await fetch_all_data(symbols, tf, limit)
            results[tf] = res
        return results
        
    return asyncio.run(run_all())
