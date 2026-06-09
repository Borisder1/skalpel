"""
SMC Agent v6 — Core SMC Logic (CLASSIC REWRITE)
Виправлена, класична SMC логіка з відкладеним підтвердженням Ордер Блоків.
"""
import numpy as np
import pandas as pd
import copy
from dataclasses import dataclass, field
from typing import Optional


# ===========================================================================
# DATA CLASSES
# ===========================================================================

@dataclass
class OrderBlock:
    valid: bool = False
    bullish: bool = True
    high: float = np.nan
    low: float = np.nan
    born_index: int = -1
    displaced: bool = False
    has_fvg: bool = False
    already_traded: bool = False


@dataclass
class LiquidityState:
    swept_high: bool = False
    swept_low: bool = False
    level_high: float = np.nan
    level_low: float = np.nan
    bar_index: int = -1


@dataclass
class StructureState:
    last_swing_high: float = np.nan
    last_swing_low: float = np.nan
    last_swing_high_index: int = -1
    last_swing_low_index: int = -1
    bos_bull: bool = False
    bos_bear: bool = False
    choch_bull: bool = False
    choch_bear: bool = False


@dataclass
class ImbalanceModel:
    stage: str = "Waiting"


@dataclass
class CandleSignals:
    bull_engulf: bool = False
    bear_engulf: bool = False
    hammer: bool = False
    shooting_star: bool = False
    morning_star: bool = False
    evening_star: bool = False

    @property
    def bull_confirm(self) -> bool:
        return self.bull_engulf or self.hammer or self.morning_star

    @property
    def bear_confirm(self) -> bool:
        return self.bear_engulf or self.shooting_star or self.evening_star


@dataclass
class BarState:
    i: int = 0
    timestamp: pd.Timestamp = None
    o: float = np.nan
    h: float = np.nan
    l: float = np.nan
    c: float = np.nan
    v: float = np.nan
    atr: float = np.nan
    atr_base: float = np.nan
    rel_vol: float = np.nan
    vol_ma: float = np.nan
    ob: OrderBlock = field(default_factory=OrderBlock)
    liq: LiquidityState = field(default_factory=LiquidityState)
    structure: StructureState = field(default_factory=StructureState)
    imbalance: ImbalanceModel = field(default_factory=ImbalanceModel)
    candles: CandleSignals = field(default_factory=CandleSignals)
    strong_breakout_vol: bool = False
    imbalance_ok: bool = False
    bull_fvg: bool = False
    bear_fvg: bool = False
    htf_trend: int = 0
    is_atr_spike: bool = False
    adr_pct: float = 0.0
    session: str = "Off"
    poc_level: float = np.nan
    poc_long_ok: bool = False
    poc_short_ok: bool = False


# ===========================================================================
# INDICATOR CALCULATIONS
# ===========================================================================

def calc_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]

    tr = np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close))
    )
    atr = np.full(len(df), np.nan)
    if len(df) < period:
        return atr

    atr[period - 1] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(period, len(df)):
        atr[i] = atr[i - 1] * (1 - alpha) + tr[i] * alpha
    return atr


def calc_sma(arr: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        window = arr[i - period + 1: i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) >= period // 2:
            result[i] = np.mean(valid)
    return result


def calc_htf_trend(df: pd.DataFrame, htf_df: pd.DataFrame, sma_period: int = 50) -> np.ndarray:
    htf_close = htf_df["close"].values
    htf_sma = calc_sma(htf_close, sma_period)
    htf_ts = htf_df["timestamp"].values

    ltf_ts = df["timestamp"].values
    trend = np.zeros(len(df), dtype=int)

    htf_idx = 0
    current_trend = 0

    for i in range(len(df)):
        ts = ltf_ts[i]
        while htf_idx + 1 < len(htf_ts) and htf_ts[htf_idx + 1] <= ts:
            htf_idx += 1
        if not np.isnan(htf_sma[htf_idx]):
            if htf_close[htf_idx] > htf_sma[htf_idx]:
                current_trend = 1
            elif htf_close[htf_idx] < htf_sma[htf_idx]:
                current_trend = -1
            else:
                current_trend = 0
        trend[i] = current_trend

    return trend


def calc_pivot_high(high: np.ndarray, length: int) -> np.ndarray:
    result = np.full(len(high), np.nan)
    for i in range(length, len(high) - length):
        window = high[i - length: i + length + 1]
        if high[i] == np.max(window) and np.sum(window == high[i]) == 1:
            result[i] = high[i]
    return result


def calc_pivot_low(low: np.ndarray, length: int) -> np.ndarray:
    result = np.full(len(low), np.nan)
    for i in range(length, len(low) - length):
        window = low[i - length: i + length + 1]
        if low[i] == np.min(window) and np.sum(window == low[i]) == 1:
            result[i] = low[i]
    return result


def calc_sessions(df: pd.DataFrame) -> np.ndarray:
    sessions = np.full(len(df), "Off", dtype=object)
    hours = df["timestamp"].dt.hour.values
    for i in range(len(df)):
        h = hours[i]
        if 7 <= h < 11:
            sessions[i] = "London"
        elif 13 <= h < 17:
            sessions[i] = "NewYork"
        elif 0 <= h < 4:
            sessions[i] = "Asia"
    return sessions


def calc_poc_simple(close: np.ndarray, volume: np.ndarray, lookback: int = 30) -> np.ndarray:
    poc = np.full(len(close), np.nan)
    for i in range(lookback, len(close)):
        window_v = volume[i - lookback: i]
        window_c = close[i - lookback: i]
        if len(window_v) > 0 and np.sum(window_v) > 0:
            poc[i] = window_c[np.argmax(window_v)]
    return poc


# ===========================================================================
# FULL BAR-BY-BAR SMC ANALYSIS (CLASSIC REWRITE)
# ===========================================================================

def analyze(
    df: pd.DataFrame,
    htf_df: pd.DataFrame,
    swing_len: int = 2,
    liquidity_lookback: int = 10,
    atr_len: int = 14,
    spike_atr_mult: float = 2.5,
    imbalance_threshold: float = 1.2,
    rel_vol_threshold: float = 1.5,
    max_imbalance_sequence_bars: int = 50,
    require_sequence: bool = False,
    poc_lookback: int = 30,
    min_adr_pct: float = 0.8,
) -> list:
    n = len(df)
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    v = df["volume"].values
    ts = df["timestamp"].values

    atr_arr = calc_atr(df, atr_len)
    atr_base_arr = calc_sma(atr_arr, 20)
    vol_ma_arr = calc_sma(v, 20)
    htf_trend_arr = calc_htf_trend(df, htf_df, sma_period=50)
    sessions_arr = calc_sessions(df)
    poc_arr = calc_poc_simple(c, v, poc_lookback)
    pivot_highs = calc_pivot_high(h, swing_len)
    pivot_lows = calc_pivot_low(l, swing_len)

    ob = OrderBlock()
    liq = LiquidityState()
    structure = StructureState()

    states = []
    min_i = max(swing_len * 2, liquidity_lookback, atr_len, 3)

    # State tracking for SMC Sequence
    last_sweep_low_idx = -1
    last_sweep_high_idx = -1

    for i in range(n):
        bar = BarState()
        bar.i = i
        bar.timestamp = pd.Timestamp(ts[i])
        bar.o, bar.h, bar.l, bar.c, bar.v = o[i], h[i], l[i], c[i], v[i]
        bar.atr = atr_arr[i]
        bar.atr_base = atr_base_arr[i] if not np.isnan(atr_base_arr[i]) else atr_arr[i]
        bar.vol_ma = vol_ma_arr[i] if not np.isnan(vol_ma_arr[i]) else 1.0
        bar.rel_vol = v[i] / max(bar.vol_ma, 1.0)
        bar.strong_breakout_vol = bar.rel_vol >= rel_vol_threshold
        bar.htf_trend = htf_trend_arr[i]
        bar.session = sessions_arr[i]
        bar.poc_level = poc_arr[i]
        bar.poc_long_ok = c[i] > poc_arr[i] if not np.isnan(poc_arr[i]) else False
        bar.poc_short_ok = c[i] < poc_arr[i] if not np.isnan(poc_arr[i]) else False

        if i < min_i:
            states.append(bar)
            continue

        bar.is_atr_spike = bar.atr > bar.atr_base * spike_atr_mult
        tf_seconds = 15 * 60
        bar.adr_pct = (bar.atr_base * np.sqrt(86400.0 / tf_seconds)) / max(c[i], 1e-10) * 100.0

        # ── 1. LIQUIDITY SWEEP ─────────────────────────────────
        liq_start = max(0, i - liquidity_lookback)
        confirmed_highest = np.max(h[liq_start:i])
        confirmed_lowest = np.min(l[liq_start:i])

        swept_high = h[i] > confirmed_highest and c[i] < confirmed_highest
        swept_low = l[i] < confirmed_lowest and c[i] > confirmed_lowest

        if swept_low:
            last_sweep_low_idx = i
        if swept_high:
            last_sweep_high_idx = i

        liq.swept_high = swept_high
        liq.swept_low = swept_low
        liq.level_high = confirmed_highest
        liq.level_low = confirmed_lowest
        liq.bar_index = i

        # ── 2. SWING STRUCTURE ──────────────────────────────────
        ph = pivot_highs[i - swing_len] if i >= swing_len else np.nan
        pl = pivot_lows[i - swing_len] if i >= swing_len else np.nan
        if not np.isnan(ph):
            structure.last_swing_high = ph
            structure.last_swing_high_index = i - swing_len
        if not np.isnan(pl):
            structure.last_swing_low = pl
            structure.last_swing_low_index = i - swing_len

        # ── 3. BOS / CHoCH (MARKET STRUCTURE SHIFT) ─────────────
        body_top = max(o[i], c[i])
        body_bottom = min(o[i], c[i])

        # A body break of the last confirmed swing point
        bos_bull_now = not np.isnan(structure.last_swing_high) and c[i] > structure.last_swing_high
        bos_bear_now = not np.isnan(structure.last_swing_low) and c[i] < structure.last_swing_low

        prev_bos_bull = structure.bos_bull
        prev_bos_bear = structure.bos_bear
        structure.bos_bull = bos_bull_now
        structure.bos_bear = bos_bear_now
        structure.choch_bull = bos_bull_now and prev_bos_bear
        structure.choch_bear = bos_bear_now and prev_bos_bull

        bull_mss = structure.bos_bull or structure.choch_bull
        bear_mss = structure.bos_bear or structure.choch_bear

        # ── 4. ORDER BLOCK FORMATION (AGGRESSIVE SCALPING) ──────
        # Invalidate old OB if price closes beyond it
        if ob.valid:
            if ob.bullish and c[i] < ob.low:
                ob.valid = False
            elif not ob.bullish and c[i] > ob.high:
                ob.valid = False
            elif i - ob.born_index > 20:  # Швидкий таймаут для скальпінгу
                ob.valid = False

        # Формуємо OB (зону інтересу) на кожному сильному імпульсі з FVG
        bull_fvg_now = i >= 2 and l[i] > h[i - 2]
        bear_fvg_now = i >= 2 and h[i] < l[i - 2]
        
        # Сильний імпульс - це або displacement свічка, або високий об'єм
        bull_disp = c[i] > o[i] and (c[i] - o[i]) > bar.atr * 0.5
        bear_disp = c[i] < o[i] and (o[i] - c[i]) > bar.atr * 0.5

        if bull_fvg_now and (bull_disp or bar.strong_breakout_vol):
            ob = OrderBlock(
                valid=True,
                bullish=True,
                high=l[i],     # Верхня межа FVG
                low=h[i-2],    # Нижня межа FVG
                born_index=i,
                displaced=True,
                has_fvg=True,
                already_traded=False
            )
        elif bear_fvg_now and (bear_disp or bar.strong_breakout_vol):
            ob = OrderBlock(
                valid=True,
                bullish=False,
                high=l[i-2],   # Верхня межа FVG
                low=h[i],      # Нижня межа FVG
                born_index=i,
                displaced=True,
                has_fvg=True,
                already_traded=False
            )

        bar.bull_fvg = bull_fvg_now
        bar.bear_fvg = bear_fvg_now

        # ── 5. CANDLES & IMBALANCE ───────────────────────────────
        cs = CandleSignals()
        if i >= 2:
            prev_body_h = max(o[i - 1], c[i - 1])
            prev_body_l = min(o[i - 1], c[i - 1])
            cs.bull_engulf = c[i] > o[i] and c[i] >= prev_body_h and o[i] <= prev_body_l
            cs.bear_engulf = c[i] < o[i] and c[i] <= prev_body_l and o[i] >= prev_body_h

            body_size = abs(c[i] - o[i])
            lower_wick = min(o[i], c[i]) - l[i]
            upper_wick = h[i] - max(o[i], c[i])
            cs.hammer = lower_wick > body_size * 1.5 and upper_wick < body_size
            cs.shooting_star = upper_wick > body_size * 1.5 and lower_wick < body_size

        local_buy_vol = v[i] if c[i] > o[i] else 0.0
        local_sell_vol = v[i] if c[i] < o[i] else 0.0
        imb_ratio = max(local_buy_vol, local_sell_vol) / max(min(local_buy_vol, local_sell_vol), 1.0)
        bar.imbalance_ok = imb_ratio >= imbalance_threshold and bar.vol_ma > 0

        # ── COPY STATE ───────────────────────────────────────────
        bar.ob = copy.deepcopy(ob)
        bar.liq = copy.deepcopy(liq)
        bar.structure = copy.deepcopy(structure)
        bar.candles = cs

        states.append(bar)

    return states
