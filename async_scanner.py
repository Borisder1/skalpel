import asyncio
import os
from datetime import datetime

import ccxt.async_support as ccxt_async


async def fetch_all_data_multi_tf(symbols, timeframes_and_limits):
    """
    Асинхронно завантажує OHLCV дані для ВСІХ символів та ВСІХ таймфреймів паралельно.
    Використовує ccxt.async_support та семафор для уникнення Rate Limit.
    
    Returns: {"15m": {"BTC/USDT:USDT": [[...], ...], ...}, "4h": {...}}
    """
    use_demo = os.getenv("BYBIT_USE_DEMO", "true").lower() == "true"
    exchange = ccxt_async.bybit({
        'enableRateLimit': True,
        'options': {'defaultType': 'linear'}
    })
    if use_demo:
        try:
            exchange.enableDemoTrading(True)
        except Exception:
            pass

    sem = asyncio.Semaphore(12)  # 12 одночасних запитів — безпечний ліміт для Bybit

    async def fetch_one(symbol, timeframe, limit):
        async with sem:
            for attempt in range(3):
                try:
                    ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                    return symbol, timeframe, ohlcv
                except ccxt_async.RateLimitExceeded:
                    await asyncio.sleep(1.5 * (attempt + 1))
                except Exception:
                    if attempt >= 2:
                        return symbol, timeframe, None
                    await asyncio.sleep(1.0)
            return symbol, timeframe, None

    # Створюємо таски для ВСІХ комбінацій символ+таймфрейм
    tasks = []
    for tf, limit in timeframes_and_limits:
        for sym in symbols:
            tasks.append(fetch_one(sym, tf, limit))

    results_raw = await asyncio.gather(*tasks)
    await exchange.close()

    # Групуємо результати по таймфрейму
    results = {tf: {} for tf, _ in timeframes_and_limits}
    for symbol, tf, ohlcv in results_raw:
        results[tf][symbol] = ohlcv

    return results


async def fetch_tickers_async(symbols):
    """
    Асинхронно завантажує тикери для ранжування по ліквідності.
    """
    exchange = ccxt_async.bybit({
        'enableRateLimit': True,
        'options': {'defaultType': 'linear'}
    })
    try:
        tickers = await exchange.fetch_tickers(symbols)
        return tickers
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Async fetch_tickers помилка: {e}")
        return {}
    finally:
        await exchange.close()


def get_market_data_parallel(symbols, timeframes_and_limits):
    """
    Синхронна обгортка для зручного виклику з головного бота.
    timeframes_and_limits = [("15m", 100), ("4h", 50)]
    Повертає: { "15m": {"BTC/USDT:USDT": [[...], ...], ...}, "4h": {...} }
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Якщо вже є працюючий event loop (напр., Jupyter)
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, fetch_all_data_multi_tf(symbols, timeframes_and_limits)).result()
        else:
            return asyncio.run(fetch_all_data_multi_tf(symbols, timeframes_and_limits))
    except RuntimeError:
        return asyncio.run(fetch_all_data_multi_tf(symbols, timeframes_and_limits))


def get_tickers_parallel(symbols):
    """
    Синхронна обгортка для асинхронного отримання тикерів.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, fetch_tickers_async(symbols)).result()
        else:
            return asyncio.run(fetch_tickers_async(symbols))
    except RuntimeError:
        return asyncio.run(fetch_tickers_async(symbols))
