#!/usr/bin/env python3
"""
=================================================================
  BYBIT DEMO TRADE AUDIT  |  Full Performance Analysis
=================================================================
Three data sources:
  1. Bybit V5 API (closed PnL) — ground truth
  2. trades_history.db (SQLite) — local bot DB
  3. trading_stats.json — PnL tracker file

Usage:
  python audit_trades.py                # auto-detect best source
  python audit_trades.py --source api   # force Bybit API
  python audit_trades.py --source db    # force SQLite DB
  python audit_trades.py --source json  # force JSON file
  python audit_trades.py --days 30      # only last 30 days
  python audit_trades.py --csv          # also export to CSV
  python audit_trades.py --list         # print detailed trade list
=================================================================
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "active_config.json")
TRADES_DB = os.path.join(BASE, "trades_history.db")
STATS_JSON = os.path.join(BASE, "trading_stats.json")


# ══════════════════════════════════════════════════════════════════
#  SOURCE 1: Bybit V5 API
# ══════════════════════════════════════════════════════════════════
def fetch_from_api(since_ts=None):
    """Fetch all closed PnL from Bybit V5 API."""
    try:
        import ccxt
    except ImportError:
        print("  [SKIP] ccxt not installed")
        return None

    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    api_key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
    api_secret = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
    base_url = cfg.get("base_url", "https://api-demo.bybit.com")

    if not api_key or not api_secret:
        print("  [SKIP] API keys empty — cannot use Bybit API source")
        return None

    exchange = ccxt.bybit({
        "enableRateLimit": True,
        "apiKey": api_key,
        "secret": api_secret,
        "urls": {"api": base_url},
        "options": {"defaultType": "future", "adjustForTimeDifference": True, "recvWindow": 10000},
    })
    try:
        exchange.enableDemoTrading(True)
    except Exception:
        pass

    try:
        exchange.fetch_time()
        print(f"  [OK] Connected to Bybit Demo ({base_url})")
    except Exception as e:
        print(f"  [FAIL] Cannot connect: {e}")
        return None

    all_records = []
    cursor = ""
    page = 0
    params_base = {"category": "linear", "limit": 100}
    if since_ts:
        params_base["startTime"] = str(int(since_ts * 1000))

    while True:
        page += 1
        params = dict(params_base)
        if cursor:
            params["cursor"] = cursor
        try:
            resp = exchange.private_get_v5_position_closed_pnl(params)
        except Exception as e:
            print(f"  [ERROR] Page {page}: {e}")
            break
        if not isinstance(resp, dict) or resp.get("retCode") != 0:
            print(f"  [ERROR] API: {resp.get('retMsg', '?') if isinstance(resp, dict) else resp}")
            break
        result = resp.get("result", {})
        records = result.get("list", [])
        all_records.extend(records)
        next_cursor = result.get("nextPageCursor", "")
        print(f"  Page {page}: {len(records)} records (total: {len(all_records)})")
        if not records or not next_cursor:
            break
        cursor = next_cursor
        time.sleep(0.25)

    if not all_records:
        return None

    all_records.sort(key=lambda r: int(r.get("updatedTime") or r.get("createdTime") or 0))
    trades = []
    for rec in all_records:
        pnl = float(rec.get("closedPnl", 0))
        side = rec.get("side", "").upper()
        close_ts = int(rec.get("updatedTime") or rec.get("createdTime") or 0)
        trades.append({
            "symbol": rec.get("symbol", "?"),
            "direction": "LONG" if side == "BUY" else "SHORT",
            "qty": float(rec.get("qty", 0)),
            "avg_entry": float(rec.get("avgEntryPrice", 0)),
            "avg_exit": float(rec.get("avgExitPrice", 0)),
            "pnl": pnl,
            "leverage": float(rec.get("leverage", 1)),
            "close_time": datetime.fromtimestamp(close_ts / 1000, tz=timezone.utc) if close_ts else None,
            "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE"),
            "source": "API",
        })
    print(f"  Fetched {len(trades)} trades from Bybit API")
    return trades


# ══════════════════════════════════════════════════════════════════
#  SOURCE 2: SQLite DB (trades_history.db)
# ══════════════════════════════════════════════════════════════════
def fetch_from_db():
    """Read trades from local SQLite database."""
    if not os.path.exists(TRADES_DB):
        print(f"  [SKIP] {TRADES_DB} not found")
        return None

    conn = sqlite3.connect(TRADES_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
    if not c.fetchone():
        conn.close()
        print("  [SKIP] No 'trades' table in DB")
        return None

    c.execute("SELECT * FROM trades ORDER BY id")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    if not rows:
        print("  [SKIP] trades table is empty")
        return None

    trades = []
    for r in rows:
        ts_str = r.get("timestamp", "")
        close_time = None
        if ts_str:
            try:
                close_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                pass

        pnl = float(r.get("pnl") or 0)
        status = r.get("status", "")
        if status in ("WIN", "LOSS"):
            result = status
        elif pnl > 0:
            result = "WIN"
        elif pnl < 0:
            result = "LOSS"
        else:
            result = status  # OPEN, CANCELLED, etc.

        trades.append({
            "symbol": r.get("symbol", "?"),
            "direction": r.get("direction", "?"),
            "qty": 0,
            "avg_entry": float(r.get("entry_price") or 0),
            "avg_exit": 0,
            "pnl": pnl,
            "leverage": 0,
            "close_time": close_time,
            "result": result,
            "source": "DB",
            "status_raw": status,
            "order_id": r.get("order_id"),
        })

    closed = [t for t in trades if t["result"] in ("WIN", "LOSS", "BE")]
    print(f"  Loaded {len(trades)} trades from DB ({len(closed)} closed)")
    return trades


# ══════════════════════════════════════════════════════════════════
#  SOURCE 3: JSON file (trading_stats.json)
# ══════════════════════════════════════════════════════════════════
def fetch_from_json():
    """Read trades from trading_stats.json."""
    if not os.path.exists(STATS_JSON):
        print(f"  [SKIP] {STATS_JSON} not found")
        return None

    with open(STATS_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw_trades = data.get("trades", [])
    if not raw_trades:
        print("  [SKIP] No trades in JSON file")
        return None

    trades = []
    for r in raw_trades:
        pnl = float(r.get("pnl", 0))
        ts = r.get("time", "")
        close_time = None
        if ts:
            try:
                close_time = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        trades.append({
            "symbol": r.get("symbol", "?"),
            "direction": r.get("direction", "?"),
            "qty": 0,
            "avg_entry": float(r.get("entry") or 0),
            "avg_exit": float(r.get("exit") or 0),
            "pnl": pnl,
            "leverage": 0,
            "close_time": close_time,
            "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE"),
            "source": "JSON",
        })

    print(f"  Loaded {len(trades)} trades from JSON")
    return trades


# ══════════════════════════════════════════════════════════════════
#  ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════════
def analyze(trades, source_label=""):
    """Full performance analysis of parsed trades."""
    # Filter only closed trades
    closed = [t for t in trades if t["result"] in ("WIN", "LOSS", "BE")]
    all_statuses = [t for t in trades]

    if not closed:
        # Show status breakdown even if no closed trades
        status_counts = defaultdict(int)
        for t in all_statuses:
            status_counts[t.get("result") or t.get("status_raw", "?")] += 1
        print(f"\n  No closed trades to analyze.")
        print(f"  All trade statuses: {dict(status_counts)}")
        return None

    total = len(closed)
    wins = [t for t in closed if t["result"] == "WIN"]
    losses = [t for t in closed if t["result"] == "LOSS"]
    breakeven = [t for t in closed if t["result"] == "BE"]

    total_pnl = sum(t["pnl"] for t in closed)
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)
    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0

    win_rate = len(wins) / total * 100
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    # Drawdown
    running = 0
    peak = 0
    max_dd = 0
    for t in closed:
        running += t["pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    first_ts = next((t["close_time"] for t in closed if t["close_time"]), None)
    last_ts = next((t["close_time"] for t in reversed(closed) if t["close_time"]), None)

    header = f"PERFORMANCE REPORT ({source_label})" if source_label else "PERFORMANCE REPORT"
    print(f"\n{'=' * 65}")
    print(f"  {header}")
    print(f"{'=' * 65}")
    if first_ts and last_ts:
        print(f"  Period:       {first_ts.strftime('%Y-%m-%d %H:%M')} -> {last_ts.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Total Trades: {total} closed ({len(all_statuses)} total incl. open/cancelled)")
    print(f"  Wins:         {len(wins)}  |  Losses: {len(losses)}  |  Breakeven: {len(breakeven)}")
    print(f"  Win Rate:     {win_rate:.1f}%")
    print(f"-" * 65)
    print(f"  Total PnL:      {total_pnl:+.4f} USDT")
    print(f"  Gross Profit:   {gross_profit:+.4f} USDT")
    print(f"  Gross Loss:     {gross_loss:+.4f} USDT")
    print(f"  Avg Win:        {avg_win:+.4f} USDT")
    print(f"  Avg Loss:       {avg_loss:+.4f} USDT")
    print(f"  Risk/Reward:    {rr_ratio:.2f}")
    print(f"  Profit Factor:  {profit_factor:.2f}")
    print(f"  Max Drawdown:   {max_dd:.4f} USDT")
    print(f"-" * 65)

    # By direction
    for d in ("LONG", "SHORT"):
        d_trades = [t for t in closed if t["direction"] == d]
        if not d_trades:
            continue
        d_wins = [t for t in d_trades if t["result"] == "WIN"]
        d_pnl = sum(t["pnl"] for t in d_trades)
        d_wr = len(d_wins) / len(d_trades) * 100
        print(f"  {d:6s}:  {len(d_trades)} trades  |  WR={d_wr:.1f}%  |  PnL={d_pnl:+.4f}")

    print()

    # Top symbols
    sym_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl": 0.0})
    for t in closed:
        s = sym_stats[t["symbol"]]
        s["total"] += 1
        if t["result"] == "WIN":
            s["wins"] += 1
        s["pnl"] += t["pnl"]

    print(f"  TOP SYMBOLS (by trade count):")
    print(f"  {'Symbol':<22s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s}")
    print(f"  " + "-" * 50)
    for sym, st in sorted(sym_stats.items(), key=lambda x: x[1]["total"], reverse=True)[:20]:
        wr = st["wins"] / st["total"] * 100 if st["total"] else 0
        print(f"  {sym:<22s} {st['total']:>6d} {wr:>5.1f}% {st['pnl']:>+12.4f}")

    # Best/Worst
    best = max(closed, key=lambda t: t["pnl"])
    worst = min(closed, key=lambda t: t["pnl"])
    ts_best = best["close_time"].strftime("%m-%d %H:%M") if best["close_time"] else "?"
    ts_worst = worst["close_time"].strftime("%m-%d %H:%M") if worst["close_time"] else "?"
    print(f"\n  Best Trade:   {best['symbol']} {best['direction']} PnL={best['pnl']:+.4f} ({ts_best})")
    print(f"  Worst Trade:  {worst['symbol']} {worst['direction']} PnL={worst['pnl']:+.4f} ({ts_worst})")

    # Streaks
    max_win_streak = max_loss_streak = cur_win = cur_loss = 0
    for t in closed:
        if t["result"] == "WIN":
            cur_win += 1; cur_loss = 0
        elif t["result"] == "LOSS":
            cur_loss += 1; cur_win = 0
        else:
            cur_win = cur_loss = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_loss_streak = max(max_loss_streak, cur_loss)

    print(f"\n  Max Win Streak:   {max_win_streak}")
    print(f"  Max Loss Streak:  {max_loss_streak}")

    # Daily breakdown
    daily = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
    for t in closed:
        if t["close_time"]:
            day = t["close_time"].strftime("%Y-%m-%d")
            daily[day]["count"] += 1
            if t["result"] == "WIN":
                daily[day]["wins"] += 1
            daily[day]["pnl"] += t["pnl"]

    if daily:
        print(f"\n  DAILY BREAKDOWN:")
        print(f"  {'Date':<12s} {'Trades':>6s} {'WR%':>6s} {'PnL':>12s}")
        print(f"  " + "-" * 40)
        for day in sorted(daily.keys()):
            d = daily[day]
            wr = d["wins"] / d["count"] * 100 if d["count"] else 0
            print(f"  {day:<12s} {d['count']:>6d} {wr:>5.1f}% {d['pnl']:>+12.4f}")

    # Status breakdown for ALL trades (incl. non-closed)
    if len(all_statuses) > len(closed):
        opens = [t for t in all_statuses if t.get("result") == "OPEN" or t.get("status_raw") == "OPEN"]
        cancelled = [t for t in all_statuses if t.get("result") == "CANCELLED" or t.get("status_raw") == "CANCELLED"]
        if opens or cancelled:
            print(f"\n  NON-CLOSED TRADES:")
            if opens:
                print(f"    OPEN: {len(opens)}")
            if cancelled:
                print(f"    CANCELLED: {len(cancelled)}")

    print(f"\n{'=' * 65}")

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "rr_ratio": rr_ratio,
        "max_drawdown": max_dd,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
    }


def print_trade_list(trades):
    closed = [t for t in trades if t["result"] in ("WIN", "LOSS", "BE")]
    print(f"\n  ALL CLOSED TRADES ({len(closed)}):")
    print(f"  {'#':>4s} {'Time (UTC)':>17s} {'Symbol':<20s} {'Dir':>5s} {'Entry':>12s} {'Exit':>12s} {'PnL':>12s} {'Result':>6s}")
    print(f"  " + "-" * 92)
    for i, t in enumerate(closed, 1):
        ts = t["close_time"].strftime("%Y-%m-%d %H:%M") if t["close_time"] else "?"
        print(f"  {i:>4d} {ts:>17s} {t['symbol']:<20s} {t['direction']:>5s} "
              f"{t['avg_entry']:>12.6f} {t['avg_exit']:>12.6f} {t['pnl']:>+12.4f} {t['result']:>6s}")


def export_csv(trades, filepath):
    keys = ["symbol", "direction", "qty", "avg_entry", "avg_exit", "pnl",
            "leverage", "close_time", "result", "source"]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            row = dict(t)
            if row.get("close_time"):
                row["close_time"] = row["close_time"].isoformat()
            w.writerow(row)
    print(f"\n  CSV exported: {filepath}")


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Bybit Demo Trade Audit")
    parser.add_argument("--source", choices=["api", "db", "json", "auto"], default="auto",
                        help="Data source: api/db/json/auto (default: auto)")
    parser.add_argument("--days", type=int, default=0, help="Only last N days (0=all)")
    parser.add_argument("--csv", action="store_true", help="Export to CSV")
    parser.add_argument("--list", action="store_true", help="Print detailed trade list")
    args = parser.parse_args()

    since_ts = None
    if args.days > 0:
        since_ts = (datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp()

    trades = None
    source_label = ""

    if args.source == "api":
        trades = fetch_from_api(since_ts)
        source_label = "Bybit API"
    elif args.source == "db":
        trades = fetch_from_db()
        source_label = "SQLite DB"
    elif args.source == "json":
        trades = fetch_from_json()
        source_label = "JSON file"
    else:
        # Auto: try API first, then DB, then JSON
        print("=== Auto-detecting data source ===")

        print("\n1) Trying Bybit API...")
        trades = fetch_from_api(since_ts)
        if trades:
            source_label = "Bybit API"
        else:
            print("\n2) Trying SQLite DB...")
            trades = fetch_from_db()
            if trades:
                source_label = "SQLite DB"
            else:
                print("\n3) Trying JSON file...")
                trades = fetch_from_json()
                if trades:
                    source_label = "JSON file"

    if not trades:
        print("\n" + "=" * 50)
        print("  NO TRADE DATA FOUND from any source.")
        print("  Possible solutions:")
        print("    1. Set BYBIT_API_KEY + BYBIT_API_SECRET env vars")
        print("    2. Run this script on the Render server (where the bot runs)")
        print("    3. Copy trades_history.db or trading_stats.json from server")
        print("=" * 50)
        return

    # Filter by days if needed and source is not API (API already filters by since_ts)
    if args.days > 0 and source_label != "Bybit API":
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        trades = [t for t in trades if not t["close_time"] or t["close_time"] >= cutoff]

    summary = analyze(trades, source_label)

    if args.list:
        print_trade_list(trades)

    if args.csv:
        csv_path = os.path.join(BASE, "trade_audit_export.csv")
        export_csv(trades, csv_path)

    if summary:
        summary_path = os.path.join(BASE, "audit_summary.json")
        summary["source"] = source_label
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"  Summary saved: {summary_path}")


if __name__ == "__main__":
    main()
