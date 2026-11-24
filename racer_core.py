import numpy as np
import pandas as pd
import copy
from dataclasses import dataclass
from smc_core import analyze as analyze_smc

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


def calc_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    rsi = np.full(len(close), 50.0)
    if len(close) <= period:
        return rsi
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(close)):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss <= 1e-10:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calc_key_zones(df: pd.DataFrame, htf_df: pd.DataFrame, live_price: float, atr_val: float) -> dict:
    """
    Розраховує ключові рівні та зони підтримки/опору (4H Supply/Demand) і зону відкату (POC/FVG),
    analogous to IQTrend Support/Resistance & Pullback Zones.
    """
    atr_safe = max(float(atr_val or 0.0), live_price * 0.005, 1e-8)
    h4 = htf_df["high"].values if len(htf_df) > 0 else df["high"].values
    l4 = htf_df["low"].values if len(htf_df) > 0 else df["low"].values
    c15 = df["close"].values
    v15 = df["volume"].values
    h15 = df["high"].values
    l15 = df["low"].values

    # 1. 4H Key Support & Resistance levels above/below live_price
    recent_h4 = h4[-30:] if len(h4) >= 30 else h4
    recent_l4 = l4[-30:] if len(l4) >= 30 else l4

    above_highs = [float(x) for x in recent_h4 if float(x) > live_price + atr_safe * 0.3]
    below_lows = [float(x) for x in recent_l4 if float(x) < live_price - atr_safe * 0.3]

    res_level = min(above_highs) if above_highs else float(np.max(recent_h4))
    if res_level <= live_price:
        res_level = live_price + atr_safe * 2.5

    sup_level = max(below_lows) if below_lows else float(np.min(recent_l4))
    if sup_level >= live_price:
        sup_level = live_price - atr_safe * 2.5

    zone_half = atr_safe * 0.35
    res_zone = (res_level - zone_half, res_level + zone_half)
    sup_zone = (sup_level - zone_half, sup_level + zone_half)

    # Рахуємо кількість дотиків (сила зони 1..3 зірки)
    res_touches = sum(1 for x in recent_h4 if abs(float(x) - res_level) <= atr_safe * 0.8)
    sup_touches = sum(1 for x in recent_l4 if abs(float(x) - sup_level) <= atr_safe * 0.8)
    res_stars = "⭐⭐⭐" if res_touches >= 3 else ("⭐⭐" if res_touches == 2 else "⭐")
    sup_stars = "⭐⭐⭐" if sup_touches >= 3 else ("⭐⭐" if sup_touches == 2 else "⭐")

    # 2. Рівень відкату (POC - Point of Control за 24 свічки + баланс свічки імпульсу)
    win_c = c15[-24:] if len(c15) >= 24 else c15
    win_v = v15[-24:] if len(v15) >= 24 else v15
    poc_price = float(win_c[int(np.argmax(win_v))]) if len(win_v) > 0 else live_price
    mid_bar = float((h15[-2] + l15[-2]) / 2.0) if len(h15) >= 2 else live_price
    pullback_low = min(poc_price, mid_bar)
    pullback_high = max(poc_price, mid_bar)

    # 3. RSI(14) на останній закритій свічці
    rsi_arr = calc_rsi(c15, 14)
    rsi_val = float(rsi_arr[-2]) if len(rsi_arr) >= 2 else 50.0

    return {
        "res_level": res_level,
        "res_zone": res_zone,
        "res_stars": res_stars,
        "res_touches": res_touches,
        "sup_level": sup_level,
        "sup_zone": sup_zone,
        "sup_stars": sup_stars,
        "sup_touches": sup_touches,
        "pullback_zone": (pullback_low, pullback_high),
        "poc_price": poc_price,
        "rsi": rsi_val,
        "dist_to_res_atr": (res_level - live_price) / atr_safe,
        "dist_to_sup_atr": (live_price - sup_level) / atr_safe,
    }

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

@dataclass(slots=True)
class Setup:
    valid: bool = False
    dir: int = 0
    entry: float = np.nan
    sl: float = np.nan
    tp1: float = np.nan
    tp2: float = np.nan
    born_bar: int = -1

@dataclass(slots=True)
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
    bos_bull: bool = False
    bos_bear: bool = False
    choch_bull: bool = False
    choch_bear: bool = False
    ob_active: bool = False
    session: str = "Off"

def analyze_racer(df: pd.DataFrame, htf_df: pd.DataFrame, config: dict):
    n = len(df)
    o, h, l, c, v = df["open"].values, df["high"].values, df["low"].values, df["close"].values, df["volume"].values
    ts = df["timestamp"].values
    
    atr = calc_atr(df, 14)
    vol_ma = calc_sma(v, 20)
    _, _, adx = calc_dmi(df, config.get("adx_len", 14))
    
    # Підключаємо структурні сигнали з smc_core (BOS/CHoCH/OB/FVG/HTF trend/session)
    smc_states = analyze_smc(
        df,
        htf_df,
        swing_len=2,
        liquidity_lookback=int(config.get("liq_lookback", 20)),
        atr_len=14,
        rel_vol_threshold=float(config.get("vol_multiplier_min", config.get("vol_mult", 1.0))),
        min_adr_pct=0.0,
    )
            
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
    vol_min = float(config.get("vol_min", config.get("vol_multiplier_min", vol_mult)))
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
            is_htf_bullish=smc_states[i].htf_trend == 1 if i < len(smc_states) else False,
            is_htf_bearish=smc_states[i].htf_trend == -1 if i < len(smc_states) else False,
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

        # Завжди обчислюємо реальний розмір FVG на свічці (для чесної діагностики та скорингу)
        if bull_fvg:
            bar.fvg_size_atr = float((l[i] - h[i-2]) / max(atr[i], 1e-10))
        elif bear_fvg:
            bar.fvg_size_atr = float((l[i-2] - h[i]) / max(atr[i], 1e-10))

        body_size = abs(c[i] - o[i])
        is_impulse_bull = c[i] > o[i] and body_size > atr[i] * 0.55 and rel_vol >= vol_min
        is_impulse_bear = c[i] < o[i] and body_size > atr[i] * 0.55 and rel_vol >= vol_min
        bar.bull_fvg = bull_fvg
        bar.bear_fvg = bear_fvg
        bar.is_impulse_bull = is_impulse_bull
        bar.is_impulse_bear = is_impulse_bear

        # OTE Setup detection + SMC structure confluence
        smc = smc_states[i] if i < len(smc_states) else None
        bos_bull = bool(smc and smc.structure.bos_bull)
        bos_bear = bool(smc and smc.structure.bos_bear)
        choch_bull = bool(smc and smc.structure.choch_bull)
        choch_bear = bool(smc and smc.structure.choch_bear)
        ob_bull = bool(smc and smc.ob.valid and smc.ob.bullish)
        ob_bear = bool(smc and smc.ob.valid and (not smc.ob.bullish))
        fvg_bull_naked = bool(smc and smc.bull_fvg)
        fvg_bear_naked = bool(smc and smc.bear_fvg)
        bar.bos_bull = bos_bull
        bar.bos_bear = bos_bear
        bar.choch_bull = choch_bull
        bar.choch_bear = choch_bear
        bar.ob_active = bool((ob_bull or ob_bear))
        bar.session = smc.session if smc else "Off"

        # Перевіряємо інвалідацію попереднього сетапу (пробиття SL, досягнення TP1 або старіння > 4 барів)
        if current_setup.valid and i > current_setup.born_bar:
            if i - current_setup.born_bar > 4:
                current_setup = Setup()
            elif current_setup.dir == 1 and (l[i] <= current_setup.sl or h[i] >= current_setup.tp1):
                current_setup = Setup()
            elif current_setup.dir == -1 and (h[i] >= current_setup.sl or l[i] <= current_setup.tp1):
                current_setup = Setup()

        # Народження нового сетапу відбувається тільки при достатньому об'ємі (rel_vol >= vol_min)
        if rel_vol >= vol_min:
            if (bull_fvg and is_impulse_bull and not bar.is_sideways and bar.is_htf_bullish
                    and (bos_bull or choch_bull) and (fvg_bull_naked or ob_bull)):
                swing_low = np.min(l[max(0, i-5):i+1])
                if not np.isnan(last_sweep_low) and (i - last_sweep_low_bar) < 10:
                    swing_low = min(swing_low, last_sweep_low)
                fvg_top = l[i]
                fvg_bot = h[i-2]

                bull_fvg_size_atr = (fvg_top - fvg_bot) / max(atr[i], 1e-10)
                bar.fvg_size_atr = bull_fvg_size_atr
                if bull_fvg_size_atr >= fvg_min_size:
                    # Вхід від поточної ціни закриття свічки імпульсу (під реальне Market-виконання)
                    entry_price = float(c[i])
                    raw_sl = min(float(fvg_bot) - atr[i] * 0.25, entry_price - atr[i] * sl_atr_mult)
                    risk = np.clip(entry_price - raw_sl, atr[i] * 1.0, atr[i] * 2.5)
                    sl_price = float(entry_price - risk)
                    tp1_price = float(entry_price + risk * tp1_rr)
                    tp2_price = float(entry_price + risk * tp2_rr)

                    current_setup = Setup(True, 1, entry_price, sl_price, tp1_price, tp2_price, i)

            elif (bear_fvg and is_impulse_bear and not bar.is_sideways and bar.is_htf_bearish
                  and (bos_bear or choch_bear) and (fvg_bear_naked or ob_bear)):
                swing_high = np.max(h[max(0, i-5):i+1])
                if not np.isnan(last_sweep_high) and (i - last_sweep_high_bar) < 10:
                    swing_high = max(swing_high, last_sweep_high)
                fvg_top = l[i-2]
                fvg_bot = h[i]

                bear_fvg_size_atr = (fvg_top - fvg_bot) / max(atr[i], 1e-10)
                bar.fvg_size_atr = bear_fvg_size_atr
                if bear_fvg_size_atr >= fvg_min_size:
                    entry_price = float(c[i])
                    raw_sl = max(float(fvg_top) + atr[i] * 0.25, entry_price + atr[i] * sl_atr_mult)
                    risk = np.clip(raw_sl - entry_price, atr[i] * 1.0, atr[i] * 2.5)
                    sl_price = float(entry_price + risk)
                    tp1_price = float(entry_price - risk * tp1_rr)
                    tp2_price = float(entry_price - risk * tp2_rr)

                    current_setup = Setup(True, -1, entry_price, sl_price, tp1_price, tp2_price, i)

        import dataclasses
        bar.setup = dataclasses.replace(current_setup)
        bars.append(bar)
        
    return bars
