"""
V9.0 Regime Filter — Визначення режиму ринку через BTC як барометр.
Блокує нові входи під час CHOP / MANIPULATION / VOLATILE ринку.
"""
import time
import numpy as np
import pandas as pd
from datetime import datetime

# Кеш результату, щоб не смикати API на кожному циклі
_regime_cache = {
    "result": None,
    "timestamp": 0,
    "ttl": 300,  # 5 хвилин кеш
}

REGIME_BAROMETER_SYMBOL = "BTC/USDT:USDT"


def _calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Обчислює ADX(14) для DataFrame з колонками high, low, close."""
    try:
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)

        plus_dm = np.zeros(len(high))
        minus_dm = np.zeros(len(high))
        tr = np.zeros(len(high))

        for i in range(1, len(high)):
            h_diff = high[i] - high[i - 1]
            l_diff = low[i - 1] - low[i]
            plus_dm[i] = h_diff if (h_diff > l_diff and h_diff > 0) else 0
            minus_dm[i] = l_diff if (l_diff > h_diff and l_diff > 0) else 0
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )

        # Wilder smoothing
        atr = np.zeros(len(high))
        plus_di_smooth = np.zeros(len(high))
        minus_di_smooth = np.zeros(len(high))

        atr[period] = np.sum(tr[1 : period + 1])
        plus_di_smooth[period] = np.sum(plus_dm[1 : period + 1])
        minus_di_smooth[period] = np.sum(minus_dm[1 : period + 1])

        for i in range(period + 1, len(high)):
            atr[i] = atr[i - 1] - (atr[i - 1] / period) + tr[i]
            plus_di_smooth[i] = plus_di_smooth[i - 1] - (plus_di_smooth[i - 1] / period) + plus_dm[i]
            minus_di_smooth[i] = minus_di_smooth[i - 1] - (minus_di_smooth[i - 1] / period) + minus_dm[i]

        plus_di = 100.0 * plus_di_smooth / np.where(atr > 0, atr, 1)
        minus_di = 100.0 * minus_di_smooth / np.where(atr > 0, atr, 1)

        dx = np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) > 0, plus_di + minus_di, 1) * 100

        # ADX = SMA of DX over period
        adx_values = np.zeros(len(high))
        if len(dx) > 2 * period:
            adx_values[2 * period] = np.mean(dx[period + 1 : 2 * period + 1])
            for i in range(2 * period + 1, len(high)):
                adx_values[i] = (adx_values[i - 1] * (period - 1) + dx[i]) / period

        return float(adx_values[-1]) if len(adx_values) > 0 else 0.0
    except Exception:
        return 0.0


def _calculate_ema_slope(df: pd.DataFrame, ema_period: int = 20, slope_bars: int = 5) -> float:
    """Обчислює нахил EMA(20) за останні slope_bars свічок у відсотках."""
    try:
        close = df["close"].values.astype(float)
        ema = np.zeros(len(close))
        ema[0] = close[0]
        k = 2.0 / (ema_period + 1)
        for i in range(1, len(close)):
            ema[i] = close[i] * k + ema[i - 1] * (1 - k)

        if len(ema) < slope_bars + 1:
            return 0.0

        ema_now = ema[-1]
        ema_prev = ema[-slope_bars - 1]
        if ema_prev == 0:
            return 0.0
        return ((ema_now - ema_prev) / ema_prev) * 100.0
    except Exception:
        return 0.0


def _calculate_atr_ratio(df: pd.DataFrame, period: int = 14) -> float:
    """Обчислює ATR ratio: поточний ATR / середній ATR за всю історію."""
    try:
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)

        tr = np.zeros(len(high))
        for i in range(1, len(high)):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )

        if len(tr) < period * 2:
            return 1.0

        current_atr = np.mean(tr[-period:])
        avg_atr = np.mean(tr[1:])  # skip first zero

        if avg_atr == 0:
            return 1.0
        return float(current_atr / avg_atr)
    except Exception:
        return 1.0


def _calculate_wick_ratio(df: pd.DataFrame, lookback: int = 10) -> float:
    """Обчислює середній wick ratio (тіні / тіло) за останні N свічок."""
    try:
        recent = df.tail(lookback)
        ratios = []
        for _, row in recent.iterrows():
            body = abs(float(row["close"]) - float(row["open"]))
            total_range = float(row["high"]) - float(row["low"])
            if total_range > 0:
                wick = total_range - body
                ratios.append(wick / total_range)
            else:
                ratios.append(0.0)
        return float(np.mean(ratios)) if ratios else 0.0
    except Exception:
        return 0.0


def _calculate_price_range_pct(df: pd.DataFrame, lookback: int = 16) -> float:
    """Обчислює відсоток зміни ціни за останні N свічок (4h lookback ≈ 16 × 15m)."""
    try:
        recent = df.tail(lookback)
        high = float(recent["high"].max())
        low = float(recent["low"].min())
        mid = (high + low) / 2
        if mid == 0:
            return 0.0
        return ((high - low) / mid) * 100.0
    except Exception:
        return 0.0


def check_market_regime(exchange, force_refresh: bool = False) -> dict:
    """
    Перевіряє режим ринку через BTC/USDT як барометр.
    
    Returns:
        dict: {
            "regime": "TREND" | "CHOP" | "VOLATILE" | "MANIPULATION",
            "allow_trading": True/False,
            "details": str,
            "adx": float,
            "ema_slope": float,
            "atr_ratio": float,
            "wick_ratio": float,
            "price_range_pct": float,
        }
    """
    global _regime_cache

    # Повертаємо кеш, якщо ще не пройшов TTL
    now = time.time()
    if not force_refresh and _regime_cache["result"] is not None:
        if (now - _regime_cache["timestamp"]) < _regime_cache["ttl"]:
            return _regime_cache["result"]

    try:
        # Завантажуємо BTC/USDT 15m, 100 свічок
        ohlcv = exchange.fetch_ohlcv(REGIME_BAROMETER_SYMBOL, "15m", limit=100)
        if not ohlcv or len(ohlcv) < 50:
            result = {
                "regime": "UNKNOWN",
                "allow_trading": True,
                "details": "Недостатньо даних BTC для визначення режиму",
                "adx": 0, "ema_slope": 0, "atr_ratio": 1, "wick_ratio": 0, "price_range_pct": 0,
                "direction_bias": "NEUTRAL",
            }
            _regime_cache["result"] = result
            _regime_cache["timestamp"] = now
            return result

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])

        # Обчислюємо метрики
        adx = _calculate_adx(df, period=14)
        ema_slope = _calculate_ema_slope(df, ema_period=20, slope_bars=5)
        atr_ratio = _calculate_atr_ratio(df, period=14)
        wick_ratio = _calculate_wick_ratio(df, lookback=10)
        price_range_pct = _calculate_price_range_pct(df, lookback=16)

        # Класифікація режиму
        regime = "TREND"
        allow_trading = True
        reasons = []

        # MANIPULATION: ADX < 15 І wick ratio > 0.70 (тіні більше 70% діапазону)
        if adx < 15 and wick_ratio > 0.70:
            regime = "MANIPULATION"
            allow_trading = False
            reasons.append(f"ADX={adx:.1f}<15 + wicks={wick_ratio:.0%}>70%")

        # CHOP: ADX < 20 АБО price range < 1% за 4h
        elif adx < 20 or price_range_pct < 1.0:
            regime = "CHOP"
            allow_trading = False
            reasons.append(f"ADX={adx:.1f}<20" if adx < 20 else f"Range={price_range_pct:.2f}%<1%")

        # VOLATILE: ATR > 2× середнього
        elif atr_ratio > 2.0:
            regime = "VOLATILE"
            allow_trading = False  # Блокуємо при надмірній волатильності
            reasons.append(f"ATR ratio={atr_ratio:.2f}>2.0")

        # TREND: ADX > 25 І EMA slope помітний
        elif adx > 25 and abs(ema_slope) > 0.1:
            regime = "TREND"
            allow_trading = True
            reasons.append(f"ADX={adx:.1f}>25, EMA slope={ema_slope:.3f}%")

        # Нейтральний (між CHOP і TREND) — дозволяємо з обережністю
        else:
            regime = "TREND"
            allow_trading = True
            reasons.append(f"ADX={adx:.1f}, neutral zone")

        details = (
            f"BTC Regime: {regime} | ADX={adx:.1f} EMA_slope={ema_slope:.3f}% "
            f"ATR_ratio={atr_ratio:.2f} Wicks={wick_ratio:.0%} Range_4h={price_range_pct:.2f}% | "
            + "; ".join(reasons)
        )

        # V10: Direction Bias — визначаємо переважний напрямок ринку
        if ema_slope > 0.3:
            direction_bias = "BULLISH"
        elif ema_slope < -0.3:
            direction_bias = "BEARISH"
        else:
            direction_bias = "NEUTRAL"

        result = {
            "regime": regime,
            "allow_trading": allow_trading,
            "details": details,
            "adx": adx,
            "ema_slope": ema_slope,
            "atr_ratio": atr_ratio,
            "wick_ratio": wick_ratio,
            "price_range_pct": price_range_pct,
            "direction_bias": direction_bias,  # V10
        }

        _regime_cache["result"] = result
        _regime_cache["timestamp"] = now

        print(f"[{datetime.now()}] 🌡️ {details} | Bias: {direction_bias}")
        return result

    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Помилка Regime Filter: {e}")
        # При помилці — дозволяємо торгувати, щоб не блокувати бота
        result = {
            "regime": "UNKNOWN",
            "allow_trading": True,
            "details": f"Помилка: {e}",
            "adx": 0, "ema_slope": 0, "atr_ratio": 1, "wick_ratio": 0, "price_range_pct": 0,
            "direction_bias": "NEUTRAL",
        }
        _regime_cache["result"] = result
        _regime_cache["timestamp"] = now
        return result


if __name__ == "__main__":
    import ccxt
    ex = ccxt.bybit({"options": {"defaultType": "swap"}})
    result = check_market_regime(ex, force_refresh=True)
    print(f"\nРежим: {result['regime']}")
    print(f"Дозволено торгувати: {result['allow_trading']}")
    print(f"Деталі: {result['details']}")
