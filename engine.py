"""
SMC Agent v6 — Trade Execution Engine
Точний переклад Pine Script логіки входу/виходу в Python.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List
from smc_core import BarState


# ===========================================================================
# PARAMS
# ===========================================================================

@dataclass
class StrategyParams:
    # Risk
    risk_atr_mult: float = 2.0
    rr_tp1: float = 1.0
    rr_tp2: float = 1.8
    rr_tp3: float = 2.8
    risk_per_trade_pct: float = 1.0
    initial_equity: float = 10_000.0

    # Filters
    min_entry_score: int = 2
    conservative_mode: bool = False
    require_sequence: bool = False
    use_poc_filter: bool = False
    use_rs_filter: bool = False

    # Session
    use_london: bool = True
    use_new_york: bool = True
    use_asia: bool = False

    # Timing
    cooldown_bars: int = 1
    min_bars_between_signals: int = 1
    max_signals_per_day: int = 50

    # Execution
    use_pullback_limits: bool = False
    require_body_momentum: bool = False
    min_body_ratio: float = 0.45
    use_early_partial_close: bool = True
    early_partial_pct: float = 25.0
    post_spike_bars_cooldown: int = 3
    spike_atr_multiplier: float = 2.5
    min_adr_pct: float = 0.8

    # Chandelier
    chandelier_len: int = 22
    chandelier_atr_mult: float = 3.0

    # Time exit
    use_time_exit: bool = False
    base_time_stop_bars: int = 28

    # NY expansion
    ny_sl_expand_factor: float = 1.35


# ===========================================================================
# TRADE RECORD
# ===========================================================================

@dataclass
class Trade:
    symbol: str = ""
    entry_bar: int = 0
    entry_time: pd.Timestamp = None
    exit_bar: int = 0
    exit_time: pd.Timestamp = None
    direction: str = "Long"   # "Long" / "Short"
    grade: str = "B"
    entry_price: float = np.nan
    stop_price: float = np.nan
    tp1: float = np.nan
    tp2: float = np.nan
    tp3: float = np.nan
    risk: float = np.nan
    qty_total: float = np.nan
    exit_price: float = np.nan
    pnl_pct: float = 0.0      # % від equity
    pnl_cash: float = 0.0
    exit_reason: str = ""
    score: int = 0
    session: str = ""
    imbalance_stage: str = ""
    bars_held: int = 0
    equity_at_entry: float = np.nan


# ===========================================================================
# BACKTEST ENGINE
# ===========================================================================

class SMCEngine:

    def __init__(self, params: StrategyParams = None):
        self.p = params or StrategyParams()
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []

    def _session_ok(self, session: str) -> bool:
        if self.p.use_london and session == "London":
            return True
        if self.p.use_new_york and session == "NewYork":
            return True
        if self.p.use_asia and session == "Asia":
            return True
        return False

    def _score_long(self, bar: BarState) -> int:
        p = self.p
        trend_ok = bar.htf_trend == 1 or (not p.conservative_mode and bar.htf_trend == 0)
        score = (
            (2 if trend_ok else 0)
            + (1 if bar.ob.displaced else 0)
            + (1 if bar.ob.has_fvg else 0)
            + (1 if bar.imbalance_ok else 0)
            + (1 if bar.strong_breakout_vol else 0)
            + (1 if bar.poc_long_ok or not p.use_poc_filter else 0)
            + (1 if bar.candles.bull_confirm else 0)
            + (1 if bar.atr > 0 else 0)   # volatility ok proxy
        )
        return score

    def _score_short(self, bar: BarState) -> int:
        p = self.p
        trend_ok = bar.htf_trend == -1 or (not p.conservative_mode and bar.htf_trend == 0)
        score = (
            (2 if trend_ok else 0)
            + (1 if bar.ob.displaced else 0)
            + (1 if bar.ob.has_fvg else 0)
            + (1 if bar.imbalance_ok else 0)
            + (1 if bar.strong_breakout_vol else 0)
            + (1 if bar.poc_short_ok or not p.use_poc_filter else 0)
            + (1 if bar.candles.bear_confirm else 0)
            + (1 if bar.atr > 0 else 0)
        )
        return score

    def _body_momentum_ok(self, bar: BarState) -> bool:
        if not self.p.require_body_momentum:
            return True
        body = abs(bar.c - bar.o)
        rng = max(bar.h - bar.l, 1e-10)
        return (body / rng) >= self.p.min_body_ratio

    def _calc_qty(self, equity: float, risk: float) -> float:
        risk_cash = equity * self.p.risk_per_trade_pct * 0.01
        return risk_cash / max(risk, 1e-10)

    def run(self, states: List[BarState], symbol: str = "BTC/USDT") -> dict:
        """
        Головний цикл бектесту.
        Returns: dict зі статистикою.
        """
        p = self.p
        equity = p.initial_equity
        self.trades = []
        self.equity_curve = [equity]

        # State tracking
        in_position = False
        pos_dir = ""
        pos_entry = np.nan
        pos_stop = np.nan
        pos_tp1 = np.nan
        pos_tp2 = np.nan
        pos_tp3 = np.nan
        pos_qty = np.nan
        pos_risk = np.nan
        pos_entry_bar = 0
        pos_entry_time = None
        pos_grade = "B"
        pos_score = 0
        pos_session = ""
        pos_stage = ""
        pos_equity_entry = equity
        pos_moved_be = False
        qty_remaining = 1.0   # fraction of original qty still open

        last_signal_bar = -9999
        signals_today = 0
        cooldown_left = 0
        signal_day = -1
        last_spike_bar = -9999
        session_peak_eq = equity
        session_max_dd = 0.0

        n = len(states)

        for i, bar in enumerate(states):
            # ── Daily reset ───────────────────────────────────────────────
            day = bar.timestamp.day if bar.timestamp is not None else i
            if day != signal_day:
                signal_day = day
                signals_today = 0
                session_peak_eq = equity
                session_max_dd = 0.0

            # ── Risk Guard ───────────────────────────────────────────────
            session_peak_eq = max(session_peak_eq, equity)
            curr_dd = (session_peak_eq - equity) / max(session_peak_eq, 1.0)
            session_max_dd = max(session_max_dd, curr_dd)
            risk_exceeded = session_max_dd >= 0.05  # 5% max DD

            # ── Spike cooldown ────────────────────────────────────────────
            if bar.is_atr_spike:
                last_spike_bar = i
            post_spike_cooldown = (i - last_spike_bar) <= p.post_spike_bars_cooldown

            # ── Cooldown counter ──────────────────────────────────────────
            if cooldown_left > 0:
                cooldown_left -= 1

            # ── Chandelier stops (for trailing) ──────────────────────────
            lookback_start = max(0, i - p.chandelier_len)
            chan_high = np.max([s.h for s in states[lookback_start:i+1]]) if i > 0 else bar.h
            chan_low  = np.min([s.l for s in states[lookback_start:i+1]]) if i > 0 else bar.l
            long_chandelier  = chan_high - bar.atr * p.chandelier_atr_mult
            short_chandelier = chan_low  + bar.atr * p.chandelier_atr_mult

            # ── MANAGE OPEN POSITION ──────────────────────────────────────
            if in_position:
                close_trade = False
                exit_price = bar.c
                exit_reason = ""
                pnl_per_unit = 0.0
                realized_pnl = 0.0

                if pos_dir == "Long":
                    # TP1 (50%)
                    if bar.h >= pos_tp1 and qty_remaining > 0.6:
                        fill = pos_tp1
                        qty_closed = 0.50
                        pnl_cash = (fill - pos_entry) * pos_qty * qty_closed
                        equity += pnl_cash
                        qty_remaining -= qty_closed
                        # Move to break-even after TP1 ONLY (not aggressive)
                        pos_stop = max(pos_entry, pos_stop)
                        pos_moved_be = True

                    # TP2 (rest 50%)
                    if bar.h >= pos_tp2 and qty_remaining > 0:
                        exit_price = pos_tp2
                        exit_reason = "TP2"
                        close_trade = True

                    # TP3 / Trail
                    if bar.h >= pos_tp3 and qty_remaining > 0:
                        exit_price = pos_tp3
                        exit_reason = "TP3"
                        close_trade = True

                    # Stop
                    effective_stop = max(pos_stop, long_chandelier) if pos_moved_be else pos_stop
                    if bar.l <= effective_stop and not close_trade:
                        exit_price = effective_stop
                        exit_reason = "SL"
                        close_trade = True

                    if close_trade and qty_remaining > 0:
                        realized_pnl = (exit_price - pos_entry) * pos_qty * qty_remaining
                        equity += realized_pnl

                elif pos_dir == "Short":
                    # TP1 (50%)
                    if bar.l <= pos_tp1 and qty_remaining > 0.6:
                        fill = pos_tp1
                        qty_closed = 0.50
                        pnl_cash = (pos_entry - fill) * pos_qty * qty_closed
                        equity += pnl_cash
                        qty_remaining -= qty_closed
                        # Move to BE
                        pos_stop = min(pos_entry, pos_stop)
                        pos_moved_be = True

                    # TP2
                    if bar.l <= pos_tp2 and qty_remaining > 0:
                        exit_price = pos_tp2
                        exit_reason = "TP2"
                        close_trade = True

                    # TP3
                    if bar.l <= pos_tp3 and qty_remaining > 0:
                        exit_price = pos_tp3
                        exit_reason = "TP3"
                        close_trade = True

                    # Stop
                    effective_stop = min(pos_stop, short_chandelier) if pos_moved_be else pos_stop
                    if bar.h >= effective_stop and not close_trade:
                        exit_price = effective_stop
                        exit_reason = "SL"
                        close_trade = True

                    if close_trade and qty_remaining > 0:
                        realized_pnl = (pos_entry - exit_price) * pos_qty * qty_remaining
                        equity += realized_pnl

                # Time exit
                if in_position and p.use_time_exit and not close_trade:
                    bars_held = i - pos_entry_bar
                    vol_factor = max(0.7, min(1.5, bar.atr / max(bar.atr_base, 1e-10)))
                    dyn_stop = int(round(p.base_time_stop_bars * vol_factor))
                    if bars_held >= dyn_stop:
                        exit_price = bar.c
                        exit_reason = "TimeExit"
                        if pos_dir == "Long" and qty_remaining > 0:
                            realized_pnl = (exit_price - pos_entry) * pos_qty * qty_remaining
                        elif pos_dir == "Short" and qty_remaining > 0:
                            realized_pnl = (pos_entry - exit_price) * pos_qty * qty_remaining
                        equity += realized_pnl
                        close_trade = True

                if close_trade:
                    total_pnl = equity - pos_equity_entry
                    t = Trade(
                        symbol=symbol,
                        entry_bar=pos_entry_bar,
                        entry_time=pos_entry_time,
                        exit_bar=i,
                        exit_time=bar.timestamp,
                        direction=pos_dir,
                        grade=pos_grade,
                        entry_price=pos_entry,
                        stop_price=pos_stop,
                        tp1=pos_tp1,
                        tp2=pos_tp2,
                        tp3=pos_tp3,
                        risk=pos_risk,
                        qty_total=pos_qty,
                        exit_price=exit_price,
                        pnl_cash=total_pnl,
                        pnl_pct=total_pnl / max(pos_equity_entry, 1.0) * 100.0,
                        exit_reason=exit_reason,
                        score=pos_score,
                        session=pos_session,
                        imbalance_stage=pos_stage,
                        bars_held=i - pos_entry_bar,
                        equity_at_entry=pos_equity_entry,
                    )
                    self.trades.append(t)
                    in_position = False
                    qty_remaining = 1.0
                    pos_moved_be = False
                    cooldown_left = p.cooldown_bars

            self.equity_curve.append(equity)

            # ── CHECK FOR NEW ENTRY ───────────────────────────────────────
            if in_position or cooldown_left > 0 or risk_exceeded:
                continue
            if not self._session_ok(bar.session):
                continue
            if (i - last_signal_bar) < p.min_bars_between_signals:
                continue
            if signals_today >= p.max_signals_per_day:
                continue
            if post_spike_cooldown:
                continue
            if bar.adr_pct < p.min_adr_pct:
                continue
            if not bar.ob.valid or bar.ob.already_traded:
                continue

            atr_v = max(bar.atr, 1e-10)
            threshold = (p.min_entry_score + 1) if p.conservative_mode else p.min_entry_score

            # LONG
            if (bar.ob.bullish
                    and bar.l <= bar.ob.high
                    and bar.h >= bar.ob.low
                    and self._score_long(bar) >= threshold
                    and self._body_momentum_ok(bar)):

                score = self._score_long(bar)
                entry = bar.ob.high if p.use_pullback_limits else bar.c
                stop = min(bar.ob.low, entry - atr_v * p.risk_atr_mult)
                risk = max(entry - stop, 1e-10)
                tp1 = entry + risk * p.rr_tp1
                tp2 = entry + risk * p.rr_tp2
                tp3 = entry + risk * p.rr_tp3
                grade = "A" if (bar.ob.displaced and bar.ob.has_fvg and bar.strong_breakout_vol and bar.candles.bull_confirm) else "B"
                qty = self._calc_qty(equity, risk)

                in_position = True
                pos_dir = "Long"
                pos_entry = entry
                pos_stop = stop
                pos_tp1, pos_tp2, pos_tp3 = tp1, tp2, tp3
                pos_risk = risk
                pos_qty = qty
                pos_entry_bar = i
                pos_entry_time = bar.timestamp
                pos_grade = grade
                pos_score = score
                pos_session = bar.session
                pos_stage = bar.imbalance.stage
                pos_equity_entry = equity
                pos_moved_be = False
                qty_remaining = 1.0
                last_signal_bar = i
                signals_today += 1

            # SHORT
            elif (not bar.ob.bullish
                    and bar.l <= bar.ob.high
                    and bar.h >= bar.ob.low
                    and self._score_short(bar) >= threshold
                    and self._body_momentum_ok(bar)):

                score = self._score_short(bar)
                entry = bar.ob.low if p.use_pullback_limits else bar.c
                stop = max(bar.ob.high, entry + atr_v * p.risk_atr_mult)
                risk = max(stop - entry, 1e-10)
                tp1 = entry - risk * p.rr_tp1
                tp2 = entry - risk * p.rr_tp2
                tp3 = entry - risk * p.rr_tp3
                grade = "A" if (bar.ob.displaced and bar.ob.has_fvg and bar.strong_breakout_vol and bar.candles.bear_confirm) else "B"
                qty = self._calc_qty(equity, risk)

                in_position = True
                pos_dir = "Short"
                pos_entry = entry
                pos_stop = stop
                pos_tp1, pos_tp2, pos_tp3 = tp1, tp2, tp3
                pos_risk = risk
                pos_qty = qty
                pos_entry_bar = i
                pos_entry_time = bar.timestamp
                pos_grade = grade
                pos_score = score
                pos_session = bar.session
                pos_stage = bar.imbalance.stage
                pos_equity_entry = equity
                pos_moved_be = False
                qty_remaining = 1.0
                last_signal_bar = i
                signals_today += 1

        # Close any open trade at end
        if in_position and len(states) > 0:
            last = states[-1]
            ep = last.c
            if pos_dir == "Long":
                realized = (ep - pos_entry) * pos_qty * qty_remaining
            else:
                realized = (pos_entry - ep) * pos_qty * qty_remaining
            equity += realized
            self.trades.append(Trade(
                symbol=symbol,
                entry_bar=pos_entry_bar,
                entry_time=pos_entry_time,
                exit_bar=len(states) - 1,
                exit_time=last.timestamp,
                direction=pos_dir,
                grade=pos_grade,
                entry_price=pos_entry,
                stop_price=pos_stop,
                tp1=pos_tp1,
                tp2=pos_tp2,
                tp3=pos_tp3,
                risk=pos_risk,
                qty_total=pos_qty,
                exit_price=ep,
                pnl_cash=equity - pos_equity_entry,
                pnl_pct=(equity - pos_equity_entry) / max(pos_equity_entry, 1.0) * 100.0,
                exit_reason="EndOfData",
                score=pos_score,
                session=pos_session,
                imbalance_stage=pos_stage,
                bars_held=len(states) - 1 - pos_entry_bar,
                equity_at_entry=pos_equity_entry,
            ))
        self.equity_curve.append(equity)

        return self._calc_stats(equity)

    def _calc_stats(self, final_equity: float) -> dict:
        p = self.p
        trades = self.trades
        n = len(trades)
        if n == 0:
            return {"total_trades": 0, "net_pnl": 0, "final_equity": final_equity}

        wins = [t for t in trades if t.pnl_cash > 0]
        losses = [t for t in trades if t.pnl_cash <= 0]
        win_rate = len(wins) / n * 100

        gross_profit = sum(t.pnl_cash for t in wins)
        gross_loss = abs(sum(t.pnl_cash for t in losses))
        profit_factor = gross_profit / max(gross_loss, 1e-10)

        net_pnl = final_equity - p.initial_equity
        net_pnl_pct = net_pnl / p.initial_equity * 100

        # Max drawdown from equity curve
        eq = np.array(self.equity_curve)
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.maximum(peak, 1.0)
        max_dd = float(np.max(dd)) * 100

        # Sharpe (simplified daily returns)
        if len(eq) > 1:
            rets = np.diff(eq) / np.maximum(eq[:-1], 1.0)
            sharpe = np.mean(rets) / max(np.std(rets), 1e-10) * np.sqrt(252 * 96)  # 96 bars/day for 15m
        else:
            sharpe = 0.0

        avg_win = np.mean([t.pnl_cash for t in wins]) if wins else 0
        avg_loss = np.mean([t.pnl_cash for t in losses]) if losses else 0

        by_session = {}
        for t in trades:
            s = t.session
            if s not in by_session:
                by_session[s] = {"n": 0, "wins": 0, "pnl": 0}
            by_session[s]["n"] += 1
            by_session[s]["wins"] += 1 if t.pnl_cash > 0 else 0
            by_session[s]["pnl"] += t.pnl_cash

        by_grade = {}
        for t in trades:
            g = t.grade
            if g not in by_grade:
                by_grade[g] = {"n": 0, "wins": 0, "pnl": 0}
            by_grade[g]["n"] += 1
            by_grade[g]["wins"] += 1 if t.pnl_cash > 0 else 0
            by_grade[g]["pnl"] += t.pnl_cash

        return {
            "total_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "net_pnl": round(net_pnl, 2),
            "net_pnl_pct": round(net_pnl_pct, 2),
            "final_equity": round(final_equity, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe": round(float(sharpe), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "by_session": by_session,
            "by_grade": by_grade,
        }
