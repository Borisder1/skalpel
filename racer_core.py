import numpy as np
import pandas as pd
from dataclasses import dataclass

def calc_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.full(len(df), np.nan)
    if len(df) < period: return atr
    atr[period - 1] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(period, len(df)):
        atr[i] = atr[i - 1] * (1 - alpha) + tr[i] * alpha
    return atr

def calc_sma(arr: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        result[i] = np.mean(arr[i - period + 1 : i + 1])
    return result

def calc_ema(arr: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    if len(arr) < period: return result
    result[period - 1] = np.mean(arr[:period])
    alpha = 2.0 / (period + 1)
    for i in range(period, len(arr)):
        result[i] = (arr[i] - result[i - 1]) * alpha + result[i - 1]
    return result

def calc_dmi(df: pd.DataFrame, period: int = 14):
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    prev_high = np.roll(high, 1)
    prev_low = np.roll(low, 1)
    prev_close = np.roll(close, 1)
    
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    plus_dm = np.where((high - prev_high > prev_low - low) & (high - prev_high > 0), high - prev_high, 0)
    minus_dm = np.where((prev_low - low > high - prev_high) & (prev_low - low > 0), prev_low - low, 0)
    
    tr_rma = np.full(len(df), np.nan)
    plus_dm_rma = np.full(len(df), np.nan)
    minus_dm_rma = np.full(len(df), np.nan)
    
    if len(df) >= period:
        tr_rma[period - 1] = np.mean(tr[:period])
        plus_dm_rma[period - 1] = np.mean(plus_dm[:period])
        minus_dm_rma[period - 1] = np.mean(minus_dm[:period])
        alpha = 1.0 / period
        for i in range(period, len(df)):
            tr_rma[i] = tr_rma[i - 1] * (1 - alpha) + tr[i] * alpha
            plus_dm_rma[i] = plus_dm_rma[i - 1] * (1 - alpha) + plus_dm[i] * alpha
            minus_dm_rma[i] = minus_dm_rma[i - 1] * (1 - alpha) + minus_dm[i] * alpha
            
    plus_di = 100 * plus_dm_rma / np.maximum(tr_rma, 1e-10)
    minus_di = 100 * minus_dm_rma / np.maximum(tr_rma, 1e-10)
    
    dx = 100 * np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, 1e-10)
    adx = np.full(len(df), np.nan)
    if len(df) >= period * 2:
        adx[period * 2 - 1] = np.mean(dx[period : period * 2])
        alpha_adx = 1.0 / period
        for i in range(period * 2, len(df)):
            adx[i] = adx[i - 1] * (1 - alpha_adx) + dx[i] * alpha_adx
            
    return plus_di, minus_di, adx

@dataclass
class Setup:
    valid: bool = False
    dir: int = 0
    entry: float = np.nan
    sl: float = np.nan
    tp1: float = np.nan
    tp2: float = np.nan
    born_bar: int = -1

@dataclass
class RacerBar:
    i: int
    timestamp: pd.Timestamp
    o: float
    h: float
    l: float
    c: float
    v: float
    atr: float
    vol_ma: float
    adx: float
    adx_threshold: float
    rel_vol: float
    fvg_size_atr: float
    is_sideways: bool
    is_htf_bullish: bool
    is_htf_bearish: bool
    setup: Setup
    bull_fvg: bool = False
    bear_fvg: bool = False
    is_impulse_bull: bool = False
    is_impulse_bear: bool = False

def analyze_racer(df: pd.DataFrame, htf_df: pd.DataFrame, config: dict):
    n = len(df)
    o, h, l, c, v = df["open"].values, df["high"].values, df["low"].values, df["close"].values, df["volume"].values
    ts = df["timestamp"].values
    
    atr = calc_atr(df, 14)
    vol_ma = calc_sma(v, 20)
    _, _, adx = calc_dmi(df, config.get("adx_len", 14))
    
    htf_c = htf_df["close"].values
    htf_ema_fast = calc_ema(htf_c, config.get("ema_fast", 50))
    htf_ema_slow = calc_ema(htf_c, config.get("ema_slow", 200))
    
    htf_trend = np.zeros(n)
    htf_ts = htf_df["timestamp"].values
    htf_idx = 0
    for i in range(n):
        while htf_idx + 1 < len(htf_ts) and htf_ts[htf_idx + 1] <= ts[i]:
            htf_idx += 1
        fast = htf_ema_fast[htf_idx]
        slow = htf_ema_slow[htf_idx]
        if not np.isnan(fast) and not np.isnan(slow):
            htf_trend[i] = 1 if fast > slow else -1 if fast < slow else 0
            
    bars = []
    
    last_sweep_high = np.nan
    last_sweep_low = np.nan
    last_sweep_high_bar = -1
    last_sweep_low_bar = -1
    
    current_setup = Setup()
    
    liq_lookback = config.get("liq_lookback", 20)
    adx_thresh = config.get("adx_thresh", 20)
    adx_min = config.get("adx_min", 12)
    adx_adaptive_window = config.get("adx_adaptive_window", 20)
    adx_adaptive_factor = config.get("adx_adaptive_factor", 0.7)
    vol_mult = config.get("vol_mult", config.get("vol_multiplier_min", 1.5))
    fib_level = config.get("fib_level", 0.618)
    fvg_min_size = config.get("fvg_min_size", 0.5)
    sl_atr_mult = config.get("sl_atr_mult", 1.5)
    tp1_rr = config.get("tp1_rr", 1.5)
    tp2_rr = config.get("tp2_rr", 3.0)

    for i in range(n):
        adx_lb = max(0, i - adx_adaptive_window + 1)
        adx_slice = adx[adx_lb:i + 1]
        _valid = adx_slice[~np.isnan(adx_slice)] if len(adx_slice) > 0 else np.array([])
        adx_avg = float(np.mean(_valid)) if len(_valid) >= 3 else np.nan
        adaptive_adx_thresh = max(float(adx_min), float(adx_avg) * float(adx_adaptive_factor)) if not np.isnan(adx_avg) else float(adx_min)
        is_side = adx[i] < adaptive_adx_thresh if not np.isnan(adx[i]) else True
        rel_vol = v[i] / max(vol_ma[i], 1e-10) if not np.isnan(vol_ma[i]) and vol_ma[i] > 0 else 0.0
        bar = RacerBar(
            i=i, timestamp=pd.Timestamp(ts[i]),
            o=o[i], h=h[i], l=l[i], c=c[i], v=v[i],
            atr=atr[i], vol_ma=vol_ma[i], adx=adx[i], adx_threshold=adaptive_adx_thresh,
            rel_vol=rel_vol, fvg_size_atr=0.0,
            is_sideways=is_side,
            is_htf_bullish=htf_trend[i] == 1,
            is_htf_bearish=htf_trend[i] == -1,
            setup=Setup()
        )
        
        if i < 20:
            bars.append(bar)
            continue
            
        liq_start = max(0, i - liq_lookback)
        highest_high = np.max(h[liq_start:i])
        lowest_low = np.min(l[liq_start:i])
        
        swept_high = h[i] > highest_high and c[i] < highest_high
        swept_low = l[i] < lowest_low and c[i] > lowest_low
        
        if swept_high:
            last_sweep_high = h[i]
            last_sweep_high_bar = i
        if swept_low:
            last_sweep_low = l[i]
            last_sweep_low_bar = i
            
        bull_fvg = i >= 2 and l[i] > h[i-2]
        bear_fvg = i >= 2 and h[i] < l[i-2]
        
        body_size = abs(c[i] - o[i])
        is_impulse_bull = c[i] > o[i] and body_size > atr[i] * 0.6 and rel_vol > vol_mult
        is_impulse_bear = c[i] < o[i] and body_size > atr[i] * 0.6 and rel_vol > vol_mult
        bar.bull_fvg = bull_fvg
        bar.bear_fvg = bear_fvg
        bar.is_impulse_bull = is_impulse_bull
        bar.is_impulse_bear = is_impulse_bear
        
        # OTE Setup detection
        if bull_fvg and is_impulse_bull and not bar.is_sideways and bar.is_htf_bullish:
            swing_low = np.min(l[max(0, i-5):i+1])
            if not np.isnan(last_sweep_low) and (i - last_sweep_low_bar) < 10:
                swing_low = last_sweep_low
            swing_high = h[i]
            fvg_top = l[i]
            fvg_bot = h[i-2]
            
            bull_fvg_size_atr = (fvg_top - fvg_bot) / max(atr[i], 1e-10)
            bar.fvg_size_atr = bull_fvg_size_atr
            if bull_fvg_size_atr >= fvg_min_size:
                fib_entry = swing_low + (swing_high - swing_low) * (1.0 - fib_level)
                entry_price = min(fib_entry, fvg_top)
                sl_price = min(swing_low, entry_price - atr[i] * sl_atr_mult)
                risk = max(entry_price - sl_price, 1e-10)
                tp1_price = entry_price + risk * tp1_rr
                tp2_price = entry_price + risk * tp2_rr
                
                current_setup = Setup(True, 1, entry_price, sl_price, tp1_price, tp2_price, i)
                
        elif bear_fvg and is_impulse_bear and not bar.is_sideways and bar.is_htf_bearish:
            swing_high = np.max(h[max(0, i-5):i+1])
            if not np.isnan(last_sweep_high) and (i - last_sweep_high_bar) < 10:
                swing_high = last_sweep_high
            swing_low = l[i]
            fvg_top = l[i-2]
            fvg_bot = h[i]
            
            bear_fvg_size_atr = (fvg_top - fvg_bot) / max(atr[i], 1e-10)
            bar.fvg_size_atr = bear_fvg_size_atr
            if bear_fvg_size_atr >= fvg_min_size:
                fib_entry = swing_high - (swing_high - swing_low) * (1.0 - fib_level)
                entry_price = max(fib_entry, fvg_bot)
                sl_price = max(swing_high, entry_price + atr[i] * sl_atr_mult)
                risk = max(sl_price - entry_price, 1e-10)
                tp1_price = entry_price - risk * tp1_rr
                tp2_price = entry_price - risk * tp2_rr
                
                current_setup = Setup(True, -1, entry_price, sl_price, tp1_price, tp2_price, i)
                
        if current_setup.valid and i - current_setup.born_bar > 15:
            current_setup = Setup()
            
        import copy
        bar.setup = copy.deepcopy(current_setup)
        bars.append(bar)
        
    return bars
