"""
SMC Agent v6 — Parameter Optimizer
Grid search по ключових параметрах з паралельним виконанням.
"""
import itertools
import time
from copy import deepcopy
from typing import List, Dict, Any

import pandas as pd
import numpy as np

from engine import SMCEngine, StrategyParams
from racer_engine import RacerEngine


# ===========================================================================
# PARAMETER GRID
# ===========================================================================

DEFAULT_GRID = {
    "risk_atr_mult":      [1.0, 1.5, 2.0],
    "rr_tp1":             [0.8, 1.0, 1.2],
    "min_entry_score":    [3, 4, 5],
    "cooldown_bars":      [2, 4],
}

FAST_GRID = {
    "risk_atr_mult":      [1.5, 2.0],
    "rr_tp1":             [0.8, 1.0],
    "min_entry_score":    [3, 4],
}

RACER_GRID = {
    "sl_atr_mult": [1.0, 1.5, 2.0],
    "fib_level": [0.5, 0.618, 0.786],
    "tp1_rr": [1.0, 1.5],
    "tp2_rr": [2.5, 3.0]
}

RACER_FAST_GRID = {
    "sl_atr_mult": [1.5, 2.0],
    "fib_level": [0.5, 0.618],
    "tp1_rr": [1.5]
}


def grid_combinations(grid: Dict[str, list]) -> List[Dict[str, Any]]:
    """Генерує всі комбінації параметрів."""
    keys = list(grid.keys())
    values = list(grid.values())
    combos = []
    for combo in itertools.product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


# ===========================================================================
# SINGLE RUN
# ===========================================================================

def run_single(
    states: list,
    symbol: str,
    param_override: Dict[str, Any],
    base_params: Any = None,
    strategy_name: str = "racer"
) -> Dict[str, Any]:
    """Запускає один бектест з заданими параметрами."""
    if strategy_name == "racer":
        config = deepcopy(base_params) if base_params else {}
        for k, v in param_override.items():
            config[k] = v
        engine = RacerEngine(config)
        stats = engine.run(states, symbol)
    else:
        p = deepcopy(base_params) if base_params else StrategyParams()
        for k, v in param_override.items():
            if hasattr(p, k):
                setattr(p, k, v)
        engine = SMCEngine(p)
        stats = engine.run(states, symbol)
        
    result = {**param_override, **stats}
    result["symbol"] = symbol
    return result


# ===========================================================================
# OPTIMIZER
# ===========================================================================

def optimize(
    states_dict: Dict[str, list],
    grid: Dict[str, list] = None,
    base_params: Any = None,
    sort_by: str = "net_pnl_pct",
    top_n: int = 20,
    verbose: bool = True,
    strategy_name: str = "racer"
) -> pd.DataFrame:
    """
    Повний перебір параметрів по всіх символах.

    Args:
        states_dict: {symbol: list[BarState]}
        grid: словник параметрів для перебору
        base_params: базові параметри
        sort_by: метрика сортування ('net_pnl_pct', 'sharpe', 'win_rate', 'profit_factor')
        top_n: кількість найкращих результатів
        verbose: виводити прогрес

    Returns:
        DataFrame з результатами, відсортований за sort_by
    """
    if grid is None:
        grid = FAST_GRID

    combos = grid_combinations(grid)
    total = len(combos) * len(states_dict)
    results = []

    if verbose:
        print(f"\n🔍 Optimizer: {len(combos)} combos × {len(states_dict)} symbols = {total} runs")
        print(f"   Sort by: {sort_by}")

    t0 = time.time()
    done = 0

    for symbol, states in states_dict.items():
        for combo in combos:
            try:
                r = run_single(states, symbol, combo, base_params, strategy_name)
                results.append(r)
            except Exception as e:
                if verbose:
                    print(f"  ⚠️  {symbol} {combo}: {e}")
            done += 1
            if verbose and done % max(total // 10, 1) == 0:
                elapsed = time.time() - t0
                eta = elapsed / done * (total - done)
                print(f"  [{done}/{total}] elapsed {elapsed:.0f}s, ETA {eta:.0f}s")

    if not results:
        print("❌ No results")
        return pd.DataFrame()

    df = pd.DataFrame(results)

    # Filter: at least 5 trades
    df = df[df["total_trades"] >= 5].copy()

    if df.empty:
        print("⚠️  All runs produced < 5 trades — loosen filters")
        return pd.DataFrame(results)

    df = df.sort_values(sort_by, ascending=False).reset_index(drop=True)

    elapsed = time.time() - t0
    if verbose:
        print(f"\n✅ Done in {elapsed:.1f}s — {len(df)} valid runs")
        print(f"\n🏆 Top {min(top_n, len(df))} by {sort_by}:")
        cols = list(grid.keys()) + ["symbol", "total_trades", "win_rate",
                                     "net_pnl_pct", "max_drawdown_pct",
                                     "profit_factor", "sharpe"]
        cols = [c for c in cols if c in df.columns]
        print(df[cols].head(top_n).to_string(index=False))

    return df.head(top_n)


# ===========================================================================
# WALK-FORWARD VALIDATION
# ===========================================================================

def walk_forward(
    states: list,
    symbol: str,
    best_params: Dict[str, Any],
    n_splits: int = 3,
    verbose: bool = True,
    strategy_name: str = "racer",
    base_params: Any = None
) -> pd.DataFrame:
    """
    Walk-forward тест: ділить дані на n_splits частин і тестує послідовно.
    """
    chunk = len(states) // n_splits
    results = []

    for split in range(n_splits):
        start = split * chunk
        end = start + chunk if split < n_splits - 1 else len(states)
        chunk_states = states[start:end]

        if strategy_name == "racer":
            config = deepcopy(base_params) if base_params else {}
            for k, v in best_params.items():
                config[k] = v
            engine = RacerEngine(config)
            stats = engine.run(chunk_states, symbol)
        else:
            p = StrategyParams()
            for k, v in best_params.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            engine = SMCEngine(p)
            stats = engine.run(chunk_states, symbol)
        stats["split"] = split + 1
        stats["bars"] = end - start
        results.append(stats)

    df = pd.DataFrame(results)
    if verbose:
        print(f"\n📊 Walk-forward ({n_splits} splits) — {symbol}:")
        cols = ["split", "bars", "total_trades", "win_rate",
                "net_pnl_pct", "max_drawdown_pct", "sharpe"]
        cols = [c for c in cols if c in df.columns]
        print(df[cols].to_string(index=False))
    return df


if __name__ == "__main__":
    print("Run via run_backtest.py --optimize")
