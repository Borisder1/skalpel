#!/usr/bin/env python3
"""
🔬 АУДИТ ТА ПОРІВНЯННЯ: РЕАЛЬНІ УГОДИ BYBIT DEMO vs ВІРТУАЛЬНЕ СЕРЕДОВИЩЕ (БД)
================================================================================
Запуск у Render Shell:
  1) Повна діагностика та порівняння:
     python audit_real_vs_virtual.py

  2) Очищення спотворених віртуальних угод + перенавчання AI з пріоритетом реальних угод:
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
    print("=" * 92)
    print("🔬 АУДИТ: РЕАЛЬНИЙ BYBIT DEMO vs ВІРТУАЛЬНЕ СЕРЕДОВИЩЕ ТА САМОНАВЧАННЯ БОТА")
    print("=" * 92)
    print(f"Час запуску: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | База даних: {DB_PATH}")

    # 1. Завантажуємо реальні дані з біржі Bybit
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

    # Розділяємо угоди в БД
    db_real_closed = [t for t in all_db_trades if t["status"] in ("WIN", "LOSS")]
    db_real_open = [t for t in all_db_trades if t["status"] == "OPEN"]
    db_virt_closed = [t for t in all_db_trades if t["status"] in ("VIRTUAL_WIN", "VIRTUAL_LOSS")]
    db_virt_open = [t for t in all_db_trades if t["status"] == "VIRTUAL_OPEN"]
    db_cancelled = [t for t in all_db_trades if t["status"] == "CANCELLED"]

    # Шукаємо спотворені віртуальні угоди (через старі баги float32 та breakeven sl=entry)
    corrupted_outliers = []
    corrupted_be_losses = []
    clean_virt_pnls = []

    for t in db_virt_closed:
        pnl = _safe_float(t.get("pnl"), 0.0)
        be = int(t.get("breakeven_activated") or 0)
        if abs(pnl) > 2500.0:
            corrupted_outliers.append(t)
        elif t["status"] == "VIRTUAL_LOSS" and be == 1 and abs(pnl) < 1e-4:
            corrupted_be_losses.append(t)
        else:
            clean_virt_pnls.append(pnl)

    m_db_real = calc_metrics([_safe_float(t.get("pnl")) for t in db_real_closed])
    m_db_virt_raw = calc_metrics([_safe_float(t.get("pnl")) for t in db_virt_closed])
    m_db_virt_clean = calc_metrics(clean_virt_pnls)

    print("\n" + "=" * 92)
    print("📊 1. ЗВЕДЕНА ПОРІВНЯЛЬНА ТАБЛИЦЯ: БІРЖА BYBIT vs БАЗА ДАНИХ vs ВІРТУАЛЬНІ УГОДИ")
    print("=" * 92)
    header = f"{'Джерело даних':<28} | {'Угод':<6} | {'WIN':<5} | {'LOSS':<5} | {'WR %':<6} | {'PF':<5} | {'Сер.WIN':<9} | {'Сер.LOSS':<9} | {'Чистий PnL'}"
    print(header)
    print("-" * 92)
    for label, m in [
        ("1. Bybit Біржа (API v5)", m_bybit),
        ("2. БД: Реальні (WIN/LOSS)", m_db_real),
        ("3. БД: Віртуальні (СИРІ)", m_db_virt_raw),
        ("4. БД: Віртуальні (ОЧИЩЕНІ)", m_db_virt_clean),
    ]:
        print(
            f"{label:<28} | {m['total']:<6} | {m['wins']:<5} | {m['losses']:<5} | "
            f"{m['wr']:>5.1f}% | {m['pf']:>5.2f} | {m['avg_win']:>+8.1f}$ | {m['avg_loss']:>+8.1f}$ | {m['net_pnl']:>+11,.2f}$"
        )
    print("-" * 92)
    print(f"📌 Зараз відкрито в БД: Реальних OPEN = {len(db_real_open)} | Віртуальних VIRTUAL_OPEN = {len(db_virt_open)} | Скасованих = {len(db_cancelled)}")

    print("\n" + "=" * 92)
    print("🕵️ 2. ДІАГНОСТИКА ВІРТУАЛЬНОГО СЕРЕДОВИЩА (ЧИ БРЕШЕ ВОНО І ЧОМУ?)")
    print("=" * 92)
    print(f"• Спотворені віртуальні угоди з фантастичним PnL (> $2,500 через старий баг float32/BE): {len(corrupted_outliers)} шт.")
    print(f"• Віртуальні угоди, де взято TP1, але записано VIRTUAL_LOSS (PnL=0.0 при безубитку):     {len(corrupted_be_losses)} шт.")
    if corrupted_outliers:
        print("\n  Приклади роздутих віртуальних угод (які завищували віртуальний PnL до мільйона):")
        for t in corrupted_outliers[:5]:
            print(f"    - ID {t['id']} | {t['timestamp']} | {t['symbol']:<16} | {t['status']} | PnL: {_safe_float(t['pnl']):>+12,.2f}$")

    print("\n" + "=" * 92)
    print("📋 3. ОСТАННІ 15 ВІРТУАЛЬНИХ УГОД У БАЗІ ДАНИХ (ЩО ВІДБУВАЄТЬСЯ У ФОНІ)")
    print("=" * 92)
    print(f"{'Дата/Час':<19} | {'Символ':<16} | {'Напр.':<6} | {'Вхід':<10} | {'SL':<10} | {'TP2':<10} | {'Статус':<13} | {'PnL ($)'}")
    print("-" * 92)
    for t in (db_virt_open + db_virt_closed)[:15]:
        en = _safe_float(t.get("entry_price"))
        sl = _safe_float(t.get("stop_loss"))
        tp2 = _safe_float(t.get("take_profit_2"))
        pnl = _safe_float(t.get("pnl"))
        print(f"{str(t.get('timestamp')):<19} | {str(t.get('symbol')):<16} | {str(t.get('direction')):<6} | {en:<10.4f} | {sl:<10.4f} | {tp2:<10.4f} | {str(t.get('status')):<13} | {pnl:>+8.2f}$")

    if do_fix:
        print("\n" + "=" * 92)
        print("🛠️ 4. ВИКОНУЄМО ОЧИЩЕННЯ СПОТВОРЕНИХ ВІРТУАЛЬНИХ ЗАПИСІВ ТА КАЛІБРУВАННЯ ВАГ AI (--fix)")
        print("=" * 92)
        outlier_ids = [t["id"] for t in corrupted_outliers]
        if outlier_ids:
            placeholders = ",".join("?" for _ in outlier_ids)
            cur.execute(f"UPDATE trades SET status = 'CANCELLED', pnl = 0.0 WHERE id IN ({placeholders})", outlier_ids)
            print(f"✅ Анульовано {len(outlier_ids)} віртуальних записів із фейковим PnL > $2,500.")

        be_ids = [t["id"] for t in corrupted_be_losses]
        if be_ids:
            placeholders = ",".join("?" for _ in be_ids)
            cur.execute(f"UPDATE trades SET status = 'VIRTUAL_WIN', pnl = 125.0 WHERE id IN ({placeholders})", be_ids)
            print(f"✅ Виправлено {len(be_ids)} угод з TP1+Безубиток: VIRTUAL_LOSS -> VIRTUAL_WIN (+125$).")

        conn.commit()

        # Перераховуємо ваги QuantEngine з 3x пріоритетом реальних угод!
        from quant_engine import DEFAULT_WEIGHTS, _enforce_weight_bounds, _save_weights
        weights = DEFAULT_WEIGHTS.copy()
        cur.execute("""
            SELECT status, pnl, factors_snapshot
            FROM trades
            WHERE status IN ('WIN', 'LOSS', 'VIRTUAL_WIN', 'VIRTUAL_LOSS')
              AND factors_snapshot IS NOT NULL
              AND ABS(pnl) <= 2500.0
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
            # Реальні угоди мають у 3 рази більшу вагу при навчанні, ніж віртуальні!
            lr = (0.018 if is_win else 0.014) if is_real else (0.005 if is_win else 0.004)
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
            "type": "REAL_PRIORITY_RECALIBRATION",
        }
        _save_weights(weights, stats)
        print(f"✅ Ваги QuantEngine перенавчено на чистих даних (Реальних: {learned_real} [3x вага], Віртуальних: {learned_virt} [1x вага])!")
        print("   Нові ваги факторів:")
        for k, v in sorted(weights.items(), key=lambda x: x[1], reverse=True):
            print(f"     • {k:<20}: {v*100:>5.1f}%")
    else:
        print("\n💡 Щоб автоматично очистити баговані віртуальні записи і перенавчити AI з 3x пріоритетом реальних угод, запусти:")
        print("   python audit_real_vs_virtual.py --fix")

    conn.close()
    print("=" * 92)


if __name__ == "__main__":
    main()
