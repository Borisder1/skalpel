import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import List

@dataclass
class Trade:
    symbol: str = "BTC/USDT"
    entry_bar: int = 0
    entry_time: pd.Timestamp = None
    exit_bar: int = 0
    exit_time: pd.Timestamp = None
    direction: str = "Long"
    entry_price: float = np.nan
    stop_price: float = np.nan
    tp1: float = np.nan
    tp2: float = np.nan
    risk: float = np.nan
    qty_total: float = np.nan
    exit_price: float = np.nan
    pnl_pct: float = 0.0
    pnl_cash: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    equity_at_entry: float = np.nan

class RacerEngine:
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []

    def run(self, bars: list, symbol: str = "BTCUSDT") -> dict:
        equity = self.config.get("initial_equity", 10000.0)
        risk_pct = self.config.get("risk_pct", 1.0)
        
        self.trades = []
        self.equity_curve = [equity]
        
        in_pos = False
        pos_dir = ""
        pos_qty = 0.0
        pos_entry = 0.0
        pos_stop = 0.0
        pos_tp1 = 0.0
        pos_tp2 = 0.0
        pos_risk = 0.0
        pos_entry_bar = 0
        pos_entry_time = None
        pos_equity = equity
        
        qty_remaining = 1.0
        
        for bar in bars:
            if in_pos:
                close_trade = False
                exit_price = bar.c
                exit_reason = ""
                
                if pos_dir == "Long":
                    if bar.h >= pos_tp1 and qty_remaining > 0.6:
                        # Close 50%
                        pnl = (pos_tp1 - pos_entry) * pos_qty * 0.5
                        equity += pnl
                        qty_remaining -= 0.5
                        pos_stop = max(pos_stop, pos_entry) # Breakeven
                        
                    if bar.h >= pos_tp2 and qty_remaining > 0:
                        exit_price = pos_tp2
                        exit_reason = "TP2"
                        close_trade = True
                        
                    if bar.l <= pos_stop and not close_trade:
                        exit_price = pos_stop
                        exit_reason = "SL"
                        close_trade = True
                        
                    if close_trade and qty_remaining > 0:
                        equity += (exit_price - pos_entry) * pos_qty * qty_remaining
                        
                elif pos_dir == "Short":
                    if bar.l <= pos_tp1 and qty_remaining > 0.6:
                        pnl = (pos_entry - pos_tp1) * pos_qty * 0.5
                        equity += pnl
                        qty_remaining -= 0.5
                        pos_stop = min(pos_stop, pos_entry)
                        
                    if bar.l <= pos_tp2 and qty_remaining > 0:
                        exit_price = pos_tp2
                        exit_reason = "TP2"
                        close_trade = True
                        
                    if bar.h >= pos_stop and not close_trade:
                        exit_price = pos_stop
                        exit_reason = "SL"
                        close_trade = True
                        
                    if close_trade and qty_remaining > 0:
                        equity += (pos_entry - exit_price) * pos_qty * qty_remaining
                        
                if close_trade:
                    total_pnl = equity - pos_equity
                    t = Trade(
                        symbol=symbol,
                        entry_bar=pos_entry_bar,
                        entry_time=pos_entry_time,
                        exit_bar=bar.i,
                        exit_time=bar.timestamp,
                        direction=pos_dir,
                        entry_price=pos_entry,
                        stop_price=pos_stop,
                        tp1=pos_tp1,
                        tp2=pos_tp2,
                        risk=pos_risk,
                        qty_total=pos_qty,
                        exit_price=exit_price,
                        pnl_cash=total_pnl,
                        pnl_pct=total_pnl / pos_equity * 100,
                        exit_reason=exit_reason,
                        bars_held=bar.i - pos_entry_bar,
                        equity_at_entry=pos_equity
                    )
                    self.trades.append(t)
                    in_pos = False
                    qty_remaining = 1.0
                    
            self.equity_curve.append(equity)
            
            # Entry logic (Limit orders simulated by checking if low <= entry for long, high >= entry for short)
            if not in_pos and bar.setup.valid:
                setup = bar.setup
                
                # Check if price hits limit entry in this bar or next bars
                if setup.dir == 1 and bar.l <= setup.entry:
                    in_pos = True
                    pos_dir = "Long"
                    pos_entry = setup.entry
                    pos_stop = setup.sl
                    pos_tp1 = setup.tp1
                    pos_tp2 = setup.tp2
                    pos_risk = pos_entry - pos_stop
                    pos_qty = (equity * risk_pct * 0.01) / max(pos_risk, 1e-10)
                    pos_entry_bar = bar.i
                    pos_entry_time = bar.timestamp
                    pos_equity = equity
                    qty_remaining = 1.0
                    setup.valid = False # Triggered
                    
                elif setup.dir == -1 and bar.h >= setup.entry:
                    in_pos = True
                    pos_dir = "Short"
                    pos_entry = setup.entry
                    pos_stop = setup.sl
                    pos_tp1 = setup.tp1
                    pos_tp2 = setup.tp2
                    pos_risk = pos_stop - pos_entry
                    pos_qty = (equity * risk_pct * 0.01) / max(pos_risk, 1e-10)
                    pos_entry_bar = bar.i
                    pos_entry_time = bar.timestamp
                    pos_equity = equity
                    qty_remaining = 1.0
                    setup.valid = False
                    
        return self._calc_stats(equity)
        
    def _calc_stats(self, final_equity):
        if not self.trades:
            return {"total_trades": 0, "net_pnl": 0}
            
        wins = [t for t in self.trades if t.pnl_cash > 0]
        losses = [t for t in self.trades if t.pnl_cash <= 0]
        
        gross_profit = sum(t.pnl_cash for t in wins)
        gross_loss = abs(sum(t.pnl_cash for t in losses))
        
        profit_factor = gross_profit / max(gross_loss, 1e-10)
        
        eq = np.array(self.equity_curve)
        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / np.maximum(peak, 1.0)
        max_dd = float(np.max(dd)) * 100
        
        return {
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self.trades) * 100, 2),
            "profit_factor": round(profit_factor, 2),
            "net_pnl": round(final_equity - self.equity_curve[0], 2),
            "net_pnl_pct": round((final_equity - self.equity_curve[0]) / self.equity_curve[0] * 100, 2),
            "max_dd_pct": round(max_dd, 2),
            "final_equity": round(final_equity, 2),
        }
