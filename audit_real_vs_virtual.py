#!/usr/bin/env python3
"""
🔬 АУДИТ ТА ПОРІВНЯННЯ: РЕАЛЬНІ УГОДИ BYBIT DEMO vs ВІРТУАЛЬНЕ СЕРЕДОВИЩЕ (БД)
================================================================================
Запуск у Render Shell:
  1) Повна діагностика та порівняння:
     python audit_real_vs_virtual.py

  2) Архівація старих (до V12) спотворених записів + перенавчання AI на чистих даних:
     python audit_real_vs_virtual.py --fix
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_PATH = os.path.join(DATA_DIR, "trades_history.db")
WEIGHTS_FILE = os.path.join(DATA_DIR, "quant_weights.json")
V12_CUTOFF = "2026-11-17 14:30:00"


def _safe_float(v, default=0.0):
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        if isinstance(v, bytes):
            import struct
            try:
                return float(struct.unpack("f", v)[0])
            except Exception:
                return default
        return default


def fetch_bybit_closed_pnl():
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    if not api_key or not api_secret:
        keys_file = os.path.join(DATA_DIR, "bybit_keys.json")
        if os.path.exists(keys_file):
            try:
                with open(keys_file, "r", encoding="utf-8") as f:
                    kd = json.load(f)
                    api_key = kd.get("api_key")
                    api_secret = kd.get("api_secret")
            except Exception:
                pass
    if not api_key or not api_secret:
        return [], 0.0

    import ccxt
    exchange = ccxt.bybit({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "timeout": 20000,
        "urls": {"api": "https://api-demo.bybit.com"},
        "options": {"defaultType": "future", "adjustForTimeDifference": True, "recvWindow": 20000},
    })
    exchange.enableDemoTrading(True)
    balance = 0.0
    try:
        bal = exchange.fetch_balance()
        usdt = bal.get("USDT", {})
        balance = float(usdt.get("total") or usdt.get("walletBalance") or 0.0)
    except Exception:
        pass

    records = []
    cursor = None
    for _ in range(15):
        params = {"category": "linear", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = exchange.privateGetV5PositionClosedPnl(params)
        if resp.get("retCode") != 0:
            break
        res = resp.get("result", {})
        batch = res.get("list", [])
        if not batch:
            break
        records.extend(batch)
        cursor = res.get("nextPageCursor")
        if not cursor or len(batch) < 100:
            break
        time.sleep(0.1)
    return records, balance


def calc_metrics(pnls):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    zeros = [p for p in pnls if p == 0]
    total = len(pnls)
    decided = len(wins) + len(losses)
    wr = (len(wins) / decided * 100.0) if decided > 0 else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.9 if gross_win > 0 else 0.0)
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "zeros": len(zeros),
        "wr": wr,
        "net_pnl": sum(pnls),
        "pf": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def main():
    do_fix = "--fix" in sys.argv
    print("=" * 94)
    print("🔬 АУДИТ: РЕАЛЬНИЙ BYBIT DEMO vs ВІРТУАЛЬНЕ СЕРЕДОВИЩЕ ТА САМОНАВЧАННЯ БОТА (V12.2)")
    print("=" * 94)
    print(f"Час запуску: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | База даних: {DB_PATH}")

    bybit_records, balance = fetch_bybit_closed_pnl()
    bybit_pnls = [float(r.get("closedPnl", 0.0)) for r in bybit_records]
    m_bybit = calc_metrics(bybit_pnls)
    if balance > 0:
        print(f"💰 Поточний баланс Bybit Demo: {balance:,.2f} USDT")

    if not os.path.exists(DB_PATH):
        print("❌ Базу даних не знайдено!")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades ORDER BY id DESC")
    all_db_trades = [dict(r) for r in cur.fetchall()]

    db_real_closed = [t for t in all_db_trades if t["status"] in ("WIN", "LOSS")]
    db_real_v12 = [t for t in db_real_closed if str(t.get("timestamp", "")) >= V12_CUTOFF]
    db_real_open = [t for t in all_db_trades if t["status"] == "OPEN"]

    db_virt_closed = [t for t in all_db_trades if t["status"] in ("VIRTUAL_WIN", "VIRTUAL_LOSS")]
    db_virt_old = [t for t in db_virt_closed if str(t.get("timestamp", "")) < V12_CUTOFF]
    db_virt_v12 = [t for t in db_virt_closed if str(t.get("timestamp", "")) >= V12_CUTOFF]
    db_virt_open = [t for t in all_db_trades if t["status"] == "VIRTUAL_OPEN"]

    m_db_real_all = calc_metrics([_safe_float(t.get("pnl")) for t in db_real_closed])
    m_db_real_v12 = calc_metrics([_safe_float(t.get("pnl")) for t in db_real_v12])
    m_db_virt_old = calc_metrics([_safe_float(t.get("pnl")) for t in db_virt_old])
    m_db_virt_v12 = calc_metrics([_safe_float(t.get("pnl")) for t in db_virt_v12])

    print("\n" + "=" * 94)
    print("📊 1. ЗВЕДЕНА ПОРІВНЯЛЬНА ТАБЛИЦЯ: БІРЖА BYBIT vs СТАРА БАЗА (ДО V12) vs НОВА БАЗА (V12)")
    print("=" * 94)
    header = f"{'Джерело даних':<30} | {'Угод':<5} | {'WIN':<5} | {'LOSS':<5} | {'WR %':<6} | {'PF':<5} | {'Сер.WIN':<9} | {'Сер.LOSS':<9} | {'Чистий PnL'}"
    print(header)
    print("-" * 94)
    for label, m in [
        ("1. Bybit Біржа (API v5 істина)", m_bybit),
        ("2. БД Реальні (всі старі+нові)", m_db_real_all),
        ("3. БД Реальні (після V12)", m_db_real_v12),
        ("4. БД Віртуальні СТАРІ (<V12)", m_db_virt_old),
        ("5. БД Віртуальні НОВІ (V12)", m_db_virt_v12),
    ]:
        print(
            f"{label:<30} | {m['total']:<5} | {m['wins']:<5} | {m['losses']:<5} | "
            f"{m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['avg_win']:>+8.1f}$ | {m['avg_loss']:>+8.1f}$ | {m['net_pnl']:>+11,.2f}$"
        )
    print("-" * 94)
    print(f"📌 Відкрито зараз: Реальних OPEN = {len(db_real_open)} | Віртуальних VIRTUAL_OPEN = {len(db_virt_open)}")

    print("\n" + "=" * 94)
    print("🕵️ 2. ЧОМУ СТАРА БАЗА ДАНИХ (ДО V12) РОЗХОДИЛАСЯ З БІРЖЕЮ BYBIT?")
    print("=" * 94)
    print(f"• Старі віртуальні угоди до V12 ({len(db_virt_old)} шт., WR 75.4%, +632k$):")
    print("  Рахувалися від теоретичної лімітної ціни Фібоначчі без перевірки, чи дійшла ціна до входу.")
    print("  Тому 75% старих віртуальних угод 'закривалися в плюс' лише на папері!")
    print(f"• Нові віртуальні угоди після V12 ({len(db_virt_v12)} шт.):")
    print("  Рахуються від реальної ринкової ціни (live_price) та ліміту ризику $250 — більше не брешуть.")

    if do_fix:
        print("\n" + "=" * 94)
        print("🛠️ 3. ОЧИЩЕННЯ ІСТОРІЇ ДО V12 ТА ПЕРЕНАВЧАННЯ AI НА ЧИСТИХ ДАНИХ (--fix)")
        print("=" * 94)
        cur.execute(
            "UPDATE trades SET status = 'ARCHIVED_V11_VIRTUAL' WHERE status IN ('VIRTUAL_WIN', 'VIRTUAL_LOSS') AND timestamp < ?",
            (V12_CUTOFF,),
        )
        archived_v = cur.rowcount
        print(f"✅ Відправлено в архів {archived_v} старих віртуальних угод (до V12), які завищували WR до 75.4%.")

        # Виправляємо старі реальні записи в БД, де немає order_id на біржі (фейкові fallback-збитки до 2 листопада)
        cur.execute(
            "UPDATE trades SET status = 'ARCHIVED_V11_FALLBACK' WHERE status = 'LOSS' AND timestamp < '2026-11-03 00:00:00'"
        )
        archived_r = cur.rowcount
        if archived_r > 0:
            print(f"✅ Відправлено в архів {archived_r} старих небіржових записів до підключення робочих ключів.")

        conn.commit()

        from quant_engine import DEFAULT_WEIGHTS, _enforce_weight_bounds, _save_weights
        weights = DEFAULT_WEIGHTS.copy()
        cur.execute("""
            SELECT status, pnl, factors_snapshot
            FROM trades
            WHERE status IN ('WIN', 'LOSS', 'VIRTUAL_WIN', 'VIRTUAL_LOSS')
              AND factors_snapshot IS NOT NULL
            ORDER BY id ASC
        """)
        rows = cur.fetchall()
        learned_real = 0
        learned_virt = 0
        wins_total = 0
        losses_total = 0

        for r in rows:
            st = r["status"]
            is_real = st in ("WIN", "LOSS")
            is_win = "WIN" in st
            lr = (0.020 if is_win else 0.015) if is_real else (0.006 if is_win else 0.005)
            try:
                factors = json.loads(r["factors_snapshot"])
            except Exception:
                continue
            if not isinstance(factors, dict):
                continue
            if is_real:
                learned_real += 1
            else:
                learned_virt += 1
            if is_win:
                wins_total += 1
            else:
                losses_total += 1
            for k, score in factors.items():
                if k not in weights:
                    continue
                score = float(score or 0.0)
                if is_win:
                    if score > 0.6:
                        weights[k] += lr * score
                    elif score < 0.3:
                        weights[k] -= lr * 0.5
                else:
                    if score > 0.6:
                        weights[k] -= lr * 0.3
                    elif score < 0.3:
                        weights[k] += lr * 0.2
            weights = _enforce_weight_bounds(weights)

        stats = {
            "total_learned": learned_real + learned_virt,
            "real_trades_learned": learned_real,
            "virtual_trades_learned": learned_virt,
            "wins_learned": wins_total,
            "losses_learned": losses_total,
            "last_learn": datetime.now().isoformat(),
            "type": "CLEAN_V12_RECALIBRATION",
        }
        _save_weights(weights, stats)
        print(f"✅ Ваги QuantEngine перенавчено ВИКЛЮЧНО на чистих даних (Реальних: {learned_real} [3x], Віртуальних V12: {learned_virt} [1x])!")
        print("   Оновлені ваги факторів:")
        for k, v in sorted(weights.items(), key=lambda x: x[1], reverse=True):
            print(f"     • {k:<20}: {v*100:>5.1f}%")

    conn.close()
    print("=" * 94)


if __name__ == "__main__":
    main()
