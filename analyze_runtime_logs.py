#!/usr/bin/env python3
"""Summarize persisted bot logs and local trade DB for quick Render-shell diagnostics."""

from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote


CYCLE_RE = re.compile(
    r"Цикл завершено \| scanned=(?P<scanned>\d+) setups=(?P<setups>\d+) "
    r"invalid=(?P<invalid>\d+) ratelimit=(?P<ratelimit>\d+) dry_cycles=(?P<dry>\d+)"
)
FILTER_RE = re.compile(
    r"ADX fail=(?P<adx>\d+)/(?:\d+) \| VOL fail=(?P<vol>\d+)/(?:\d+) \| "
    r"FVG fail=(?P<fvg>\d+)/(?:\d+) \| Пройшли всі=(?P<passed>\d+)/(?:\d+)"
)
SIGNAL_RE = re.compile(r"⚡\s+(?P<side>🟢 LONG|🔴 SHORT) \| (?P<symbol>[^\s|]+)")
ORDER_RE = re.compile(r"ОРДЕР ВИСТАВЛЕНО НА DEMO|ДЕМО-ОРДЕР")
ERR_RE = re.compile(r"Помилка|ERROR|Traceback|retCode|Risk guard stop|Rate limit", re.IGNORECASE)
SKIP_CANDLES_RE = re.compile(r"Пропускаємо (?P<symbol>\S+) (?P<tf>\S+): мало свічок \((?P<count>\d+)\)")


def decode_line(line: str) -> str:
    # Render web terminal copies sometimes URL-encode long chunks. Decode only if it looks encoded.
    if "%20" in line or "%D0" in line or "%F0" in line or "%0D%0A" in line:
        return unquote(line)
    return line


def iter_lines(paths: list[str]):
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                decoded = decode_line(raw.rstrip("\n"))
                for part in decoded.replace("\r\n", "\n").splitlines():
                    yield path, part


def summarize_logs(paths: list[str]) -> dict:
    cycles = []
    filters = []
    signals = Counter()
    errors = []
    candle_skips = Counter()
    orders = 0
    first_ts = None
    last_ts = None

    for path, line in iter_lines(paths):
        if first_ts is None:
            first_ts = line[:19]
        last_ts = line[:19]

        if m := CYCLE_RE.search(line):
            cycles.append({k: int(v) for k, v in m.groupdict().items()})
        if m := FILTER_RE.search(line):
            filters.append({k: int(v) for k, v in m.groupdict().items()})
        if m := SIGNAL_RE.search(line):
            signals[f"{m.group('side')} {m.group('symbol')}"] += 1
        if ORDER_RE.search(line):
            orders += 1
        if m := SKIP_CANDLES_RE.search(line):
            candle_skips[f"{m.group('symbol')} {m.group('tf')}"] += 1
        if ERR_RE.search(line):
            errors.append(line)

    return {
        "files": paths,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "cycles": cycles,
        "filters": filters,
        "signals": signals,
        "orders": orders,
        "errors": errors[-20:],
        "candle_skips": candle_skips,
    }


def summarize_db(db_path: str) -> str:
    if not os.path.exists(db_path):
        return "DB: trades_history.db не знайдено"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*), COALESCE(SUM(pnl), 0) FROM trades GROUP BY status").fetchall()
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades").fetchone()
    parts = [f"DB trades: total={total[0]} pnl={float(total[1]):+.4f}"]
    for status, count, pnl in rows:
        parts.append(f"{status}={count} pnl={float(pnl):+.4f}")
    return " | ".join(parts)


def render_report(summary: dict, db_path: str) -> str:
    cycles = summary["cycles"]
    filters = summary["filters"]
    total_cycles = len(cycles)
    total_scanned = sum(c["scanned"] for c in cycles)
    total_setups = sum(c["setups"] for c in cycles)
    max_dry = max((c["dry"] for c in cycles), default=0)
    total_invalid = sum(c["invalid"] for c in cycles)
    total_rl = sum(c["ratelimit"] for c in cycles)
    adx = sum(f["adx"] for f in filters)
    vol = sum(f["vol"] for f in filters)
    fvg = sum(f["fvg"] for f in filters)
    passed = sum(f["passed"] for f in filters)

    lines = [
        "📋 ОПЕРАЦІЙНИЙ АНАЛІЗ ЛОГІВ",
        f"Файли: {', '.join(summary['files'])}",
        f"Період у файлах: {summary['first_ts']} → {summary['last_ts']}",
        f"Циклів: {total_cycles} | scanned={total_scanned} | setups={total_setups} | setups/cycle={(total_setups / total_cycles if total_cycles else 0):.2f}",
        f"Invalid={total_invalid} | RateLimit={total_rl} | Max dry={max_dry}",
        f"Фільтри fail: ADX={adx}, VOL={vol}, FVG={fvg}, passed_all={passed}",
        f"Сигналів у логах: {sum(summary['signals'].values())} | Ордерних повідомлень: {summary['orders']}",
        summarize_db(db_path),
    ]

    if summary["signals"]:
        lines.append("ТОП сигналів: " + "; ".join(f"{k}×{v}" for k, v in summary["signals"].most_common(10)))
    if summary["candle_skips"]:
        lines.append("Мало свічок TOP: " + "; ".join(f"{k}×{v}" for k, v in summary["candle_skips"].most_common(8)))
    if summary["errors"]:
        lines.append("Останні помилки/ризики:")
        lines.extend(f"- {e}" for e in summary["errors"][-10:])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze bot log files from logs/*.log")
    parser.add_argument("--logs", nargs="*", default=None, help="Explicit log files. Default: logs/*.log")
    parser.add_argument("--db", default="trades_history.db")
    args = parser.parse_args()

    paths = args.logs or sorted(glob.glob("logs/*.log"))
    if not paths:
        print("❌ logs/*.log не знайдено")
        return 1
    print(render_report(summarize_logs(paths), args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
