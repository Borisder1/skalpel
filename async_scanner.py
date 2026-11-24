import concurrent.futures
import requests

_SESSION = requests.Session()
_TF_MAP = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}


def _to_market_id(symbol: str) -> str:
    return symbol.replace("/USDT:USDT", "USDT").replace(":USDT", "").replace("/", "")


def _fetch_kline_direct(symbol: str, timeframe: str, limit: int = 80):
    market_id = _to_market_id(symbol)
    interval = _TF_MAP.get(timeframe, "15")
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": market_id, "interval": interval, "limit": min(limit, 85)}
    for attempt in range(2):
        try:
            r = _SESSION.get(url, params=params, timeout=6)
            if r.status_code == 200:
                data = r.json()
                if data.get("retCode") == 0:
                    raw_list = data.get("result", {}).get("list", [])
                    if raw_list:
                        # Bybit повертає від новіших до старіших -> розвертаємо хронологічно
                        ohlcv = [
                            [int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
                            for row in reversed(raw_list)
                        ]
                        return symbol, timeframe, ohlcv
        except Exception:
            if attempt >= 1:
                return symbol, timeframe, None
    return symbol, timeframe, None


def get_market_data_parallel(symbols, timeframes_and_limits, exchange=None):
    """
    Надлегкий паралельний завантажувач свічок напряму через публічний REST Bybit V5.
    Не використовує внутрішні кеші CCXT (0 МБ витоків пам'яті, стабільна робота в 512MB RAM).
    """
    results = {tf: {} for tf, _ in timeframes_and_limits}
    if not symbols:
        return results

    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for tf, limit in timeframes_and_limits:
            capped_limit = min(int(limit), 85)
            for sym in symbols:
                tasks.append(pool.submit(_fetch_kline_direct, sym, tf, capped_limit))
        for fut in concurrent.futures.as_completed(tasks):
            try:
                sym, tf, ohlcv = fut.result()
                if ohlcv:
                    results[tf][sym] = ohlcv
            except Exception:
                pass
    return results


def fetch_derivatives_snapshot(symbol: str) -> dict:
    """Отримує Funding Rate та Open Interest з Bybit V5 (як у терміналі IQTrend)."""
    market_id = _to_market_id(symbol)
    try:
        r = _SESSION.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": market_id},
            timeout=5,
        )
        if r.status_code == 200:
            lst = r.json().get("result", {}).get("list", [])
            if lst:
                item = lst[0]
                fr = float(item.get("fundingRate") or 0.0) * 100.0
                oi = float(item.get("openInterestValue") or item.get("openInterest") or 0.0)
                return {"funding_pct": fr, "open_interest_usd": oi}
    except Exception:
        pass
    return {"funding_pct": 0.0, "open_interest_usd": 0.0}


def get_tickers_parallel(symbols, exchange=None):
    return {}
