"""
SMC Agent v6 — Main Backtest Runner
=====================================
Використання:
  python run_backtest.py
  python run_backtest.py --symbols BTCUSDT ETHUSDT DOGEUSDT --days 90
  python run_backtest.py --symbols BTCUSDT --days 180 --optimize
  python run_backtest.py --csv mydata.csv --symbol BTCUSDT --days 90

Опції:
  --symbols     Список символів (без /)  [BTCUSDT ETHUSDT DOGEUSDT]
  --days        Кількість днів (3m=90, 6m=180, 12m=365)
  --tf          Таймфрейм [15m]
  --equity      Початковий депозит [$10000]
  --optimize    Запустити оптимізатор параметрів
  --fast        Швидкий тест (менший grid)
  --csv         Шлях до CSV файлу (один символ)
  --no-report   Не генерувати HTML-звіт
  --no-browser  Не відкривати звіт у браузері
"""
import argparse
import os
import sys
import webbrowser
from datetime import datetime

# ── Imports ──────────────────────────────────────────────────────────────────
try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("❌ pandas/numpy не встановлено. Запустіть:\n   pip install pandas numpy ccxt plotly jinja2")
    sys.exit(1)

from data_fetcher import fetch_ohlcv, fetch_htf_ohlcv, load_csv, SYMBOLS
from smc_core import analyze as smc_analyze
from engine import SMCEngine, StrategyParams
from racer_core import analyze_racer
from racer_engine import RacerEngine
from optimizer import optimize, walk_forward, FAST_GRID, DEFAULT_GRID, RACER_GRID, RACER_FAST_GRID
from report import generate_report


# ===========================================================================
# SYMBOL MAP  (CLI name → ccxt name)
# ===========================================================================
SYMBOL_MAP = {
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
    "DOGEUSDT": "DOGE/USDT",
    "SOLUSDT": "SOL/USDT",
    "BNBUSDT": "BNB/USDT",
    "XRPUSDT": "XRP/USDT",
    "ADAUSDT": "ADA/USDT",
}


def resolve_symbol(s: str) -> str:
    return SYMBOL_MAP.get(s.upper(), s.replace("_", "/").upper())


# ===========================================================================
# RUN ONE SYMBOL
# ===========================================================================

def run_symbol(
    symbol_ccxt: str,
    days: int,
    tf: str,
    params: StrategyParams,
    csv_path: str = None,
    strategy_name: str = "racer"
) -> dict:
    print(f"\n{'='*60}")
    print(f"  {symbol_ccxt} | {tf} | {days} days")
    print(f"{'='*60}")

    # 1. Load data
    if csv_path:
        df = load_csv(csv_path)
        # Trim to days
        cutoff = df["timestamp"].max() - pd.Timedelta(days=days)
        df = df[df["timestamp"] >= cutoff].reset_index(drop=True)
    else:
        df = fetch_ohlcv(symbol_ccxt, tf, days)

    if len(df) < 100:
        print(f"  ⚠️  Only {len(df)} bars — skipping")
        return {}

    # 2. HTF data (4H trend)
    print("  📈 Fetching HTF trend (4H)...")
    if csv_path:
        from data_fetcher import resample_to_htf
        htf_df = resample_to_htf(df, "4h")
    else:
        htf_df = fetch_htf_ohlcv(symbol_ccxt, "4h", days + 30)

    # 3. Analysis & Backtest
    if strategy_name == "racer":
        print("  🔍 Running Racer analysis...")
        config = {
            "initial_equity": params.initial_equity,
            "risk_pct": 1.0,
            "liq_lookback": 20,
            "adx_thresh": 20,
            "vol_mult": 1.5,
            "fib_level": 0.618,
            "fvg_min_size": 0.5,
            "sl_atr_mult": params.risk_atr_mult,
            "tp1_rr": params.rr_tp1,
            "tp2_rr": params.rr_tp2
        }
        states = analyze_racer(df, htf_df, config)
        print(f"  ✅ {len(states)} bars analyzed")
        print("  ⚙️  Running Racer backtest...")
        engine = RacerEngine(config)
        stats = engine.run(states, symbol_ccxt)
        trades = engine.trades
        eq_curve = engine.equity_curve
    else:
        print("  🔍 Running SMC analysis (v6)...")
        states = smc_analyze(df, htf_df)
        print(f"  ✅ {len(states)} bars analyzed")
        print("  ⚙️  Running backtest...")
        engine = SMCEngine(params)
        stats = engine.run(states, symbol_ccxt)
        trades = engine.trades
        eq_curve = engine.equity_curve

    # 5. Print summary
    print(f"\n  📊 Results:")
    print(f"     Trades:       {stats.get('total_trades', 0)}")
    print(f"     Win Rate:     {stats.get('win_rate', 0):.1f}%")
    print(f"     Net PnL:      ${stats.get('net_pnl', 0):+.2f} ({stats.get('net_pnl_pct', 0):+.2f}%)")
    print(f"     Max DD:       {stats.get('max_drawdown_pct', 0):.2f}%")
    print(f"     Profit Factor:{stats.get('profit_factor', 0):.2f}")
    print(f"     Sharpe:       {stats.get('sharpe', 0):.2f}")

    if stats.get("by_session"):
        print(f"\n  📍 By Session:")
        for sess, sv in stats["by_session"].items():
            wr = sv["wins"] / max(sv["n"], 1) * 100
            print(f"     {sess:10s}  n={sv['n']:3d}  wr={wr:.0f}%  pnl=${sv['pnl']:+.2f}")

    if stats.get("by_grade"):
        print(f"\n  🏅 By Grade:")
        for grade, gv in stats["by_grade"].items():
            wr = gv["wins"] / max(gv["n"], 1) * 100
            print(f"     Grade {grade}    n={gv['n']:3d}  wr={wr:.0f}%  pnl=${gv['pnl']:+.2f}")

    return {
        "symbol": symbol_ccxt,
        "period": days,
        "stats": stats,
        "trades": trades,
        "equity_curve": eq_curve,
        "params": {
            "risk_atr_mult": params.risk_atr_mult,
            "rr_tp1": params.rr_tp1,
            "rr_tp2": params.rr_tp2,
            "rr_tp3": params.rr_tp3,
            "min_entry_score": params.min_entry_score,
            "cooldown_bars": params.cooldown_bars,
            "min_bars_between_signals": params.min_bars_between_signals,
            "use_pullback_limits": params.use_pullback_limits,
            "initial_equity": params.initial_equity,
        },
    }


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="SMC Agent v6 — Local Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "DOGEUSDT"],
                        help="Символи для тесту (BTCUSDT ETHUSDT DOGEUSDT)")
    parser.add_argument("--days", type=int, nargs="+", default=[90, 180, 365],
                        help="Кількість днів: 90=3m, 180=6m, 365=12m")
    parser.add_argument("--tf", default="15m", help="Таймфрейм [15m]")
    parser.add_argument("--equity", type=float, default=10_000.0, help="Початковий депозит")
    parser.add_argument("--optimize", action="store_true", help="Оптимізатор параметрів")
    parser.add_argument("--fast", action="store_true", help="Швидкий grid (менше комбінацій)")
    parser.add_argument("--csv", default=None, help="Шлях до CSV (один символ)")
    parser.add_argument("--no-report", action="store_true", help="Не генерувати HTML")
    parser.add_argument("--no-browser", action="store_true", help="Не відкривати браузер")
    parser.add_argument("--strategy", default="racer", help="Стратегія: racer або v6")

    # Strategy params
    parser.add_argument("--risk-mult", type=float, default=2.0, help="SL ATR Mult [2.0]")
    parser.add_argument("--tp1", type=float, default=1.0, help="TP1 RR [1.0]")
    parser.add_argument("--tp2", type=float, default=1.8, help="TP2 RR [1.8]")
    parser.add_argument("--tp3", type=float, default=2.8, help="TP3 RR [2.8]")
    parser.add_argument("--min-score", type=int, default=5, help="Min Entry Score [5]")
    parser.add_argument("--cooldown", type=int, default=4, help="Cooldown Bars [4]")
    parser.add_argument("--conservative", action="store_true", help="Conservative mode")

    args = parser.parse_args()

    print("\n🤖 SMC Agent v6 — Local Backtester")
    print("=" * 60)
    print(f"  Symbols:  {args.symbols}")
    print(f"  Periods:  {args.days} days")
    print(f"  TF:       {args.tf}")
    print(f"  Equity:   ${args.equity:,.0f}")
    print(f"  Score:    {args.min_score}")
    print(f"  Optimize: {args.optimize}")

    params = StrategyParams(
        risk_atr_mult=args.risk_mult,
        rr_tp1=args.tp1,
        rr_tp2=args.tp2,
        rr_tp3=args.tp3,
        min_entry_score=args.min_score,
        cooldown_bars=args.cooldown,
        conservative_mode=args.conservative,
        initial_equity=args.equity,
    )

    all_results = []

    # ── OPTIMIZER MODE ────────────────────────────────────────────────────
    if args.optimize:
        print("\n🔍 OPTIMIZER MODE")
        if args.strategy == "racer":
            grid = RACER_FAST_GRID if args.fast else RACER_GRID
            base_config = {
                "initial_equity": args.equity,
                "risk_pct": 1.0,
                "liq_lookback": 20,
                "adx_thresh": 20,
                "vol_mult": 1.5,
                "fvg_min_size": 0.5,
            }
            opt_base_params = base_config
        else:
            grid = FAST_GRID if args.fast else DEFAULT_GRID
            opt_base_params = params
            
        print(f"  Grid: {grid}")

        # Use first symbol + first period for optimization
        sym_ccxt = resolve_symbol(args.symbols[0])
        days = args.days[0]

        if args.csv:
            df = load_csv(args.csv)
            from data_fetcher import resample_to_htf
            htf_df = resample_to_htf(df, "4h")
        else:
            df = fetch_ohlcv(sym_ccxt, args.tf, days)
            htf_df = fetch_htf_ohlcv(sym_ccxt, "4h", days + 30)

        if args.strategy == "racer":
            states = analyze_racer(df, htf_df, opt_base_params)
        else:
            states = smc_analyze(df, htf_df)
            
        opt_df = optimize(
            {sym_ccxt: states},
            grid=grid,
            base_params=opt_base_params,
            sort_by="net_pnl_pct",
            top_n=20,
            strategy_name=args.strategy
        )

        if not opt_df.empty:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            opt_csv = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "reports",
                f"optimizer_{args.strategy}_{ts}.csv",
            )
            os.makedirs(os.path.dirname(opt_csv), exist_ok=True)
            opt_df.to_csv(opt_csv, index=False)
            print(f"\n💾 Optimizer results: {opt_csv}")

            # Walk-forward with best params
            best = opt_df.iloc[0].to_dict()
            print(f"\n📊 Best params: {best}")
            walk_forward(states, sym_ccxt, best, n_splits=3, strategy_name=args.strategy, base_params=opt_base_params)

        return

    # ── BACKTEST MODE ─────────────────────────────────────────────────────
    for sym_str in args.symbols:
        sym_ccxt = resolve_symbol(sym_str)
        for days in args.days:
            result = run_symbol(
                sym_ccxt, days, args.tf, params,
                csv_path=args.csv if len(args.symbols) == 1 else None,
                strategy_name=args.strategy
            )
            if result:
                all_results.append(result)

    if not all_results:
        print("\n❌ Немає результатів. Перевірте підключення або дані.")
        return

    # ── REPORT ────────────────────────────────────────────────────────────
    if not args.no_report:
        try:
            report_path = generate_report(all_results)
            if not args.no_browser:
                webbrowser.open(f"file://{os.path.abspath(report_path)}")
        except Exception as e:
            print(f"\n⚠️  Report error: {e}")
            print("   Встановіть plotly: pip install plotly")

    print("\n✅ Done!")


if __name__ == "__main__":
    main()
