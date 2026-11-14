#!/usr/bin/env python3
"""
📊 Bybit Demo Trade History & PnL Exporter
===========================================
Витягує реальну історію торгів, закритих позицій та PnL з Bybit Demo акаунта через офіційне API v5.
Зберігає результати у CSV та JSON на постійний диск /data/ та показує детальну зведену таблицю.

Запуск на Render:
  python export_bybit_trades.py
"""

import os
import sys
import json
import csv
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

def export_trades():
    print("=" * 80)
    print("📈 BYBIT DEMO TRADE & PNL EXPORTER")
    print("=" * 80)
    print(f"Час запуску: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Знаходимо API ключі
    _data_dir = "/data" if os.path.isdir("/data") else "."
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")

    if not api_key or not api_secret:
        keys_file = os.path.join(_data_dir, "bybit_keys.json")
        if os.path.exists(keys_file):
            try:
                with open(keys_file, "r", encoding="utf-8") as kf:
                    kdata = json.load(kf)
                    api_key = kdata.get("api_key")
                    api_secret = kdata.get("api_secret")
                    print(f"🔑 Завантажено ключі з {keys_file}")
            except Exception as e:
                print(f"⚠️ Помилка читання {keys_file}: {e}")

    if not api_key or not api_secret:
        print("❌ Помилка: Bybit API ключі не знайдені в Environment Variables або bybit_keys.json!")
        return

    print(f"🔑 Використовуємо ключ: ...{api_key[-4:]}")

    # 2. Підключаємося через CCXT
    try:
        import ccxt
    except ImportError:
        print("❌ Бібліотека ccxt не встановлена. Встановіть: pip install ccxt")
        return

    exchange = ccxt.bybit({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'timeout': 20000,
        'urls': {'api': "https://api-demo.bybit.com"},
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
            'recvWindow': 20000
        }
    })
    exchange.enableDemoTrading(True)
    try:
        exchange.load_time_difference()
    except Exception:
        pass

    # 3. Перевіримо поточний баланс
    try:
        bal = exchange.fetch_balance()
        usdt_bal = bal.get("USDT", {})
        total_balance = float(usdt_bal.get("total") or usdt_bal.get("walletBalance") or 0.0)
        free_balance = float(usdt_bal.get("free") or usdt_bal.get("availableToWithdraw") or 0.0)
        print(f"💰 Поточний баланс Bybit Demo: {total_balance:,.2f} USDT (Вільна маржа: {free_balance:,.2f} USDT)\n")
    except Exception as e_bal:
        print(f"⚠️ Не вдалося отримати поточний баланс: {e_bal}\n")

    # 4. Витягуємо Closed PnL (закриті позиції за API v5)
    print("⏳ Отримання історії закритих позицій (Closed PnL)...")
    closed_pnl_records = []
    try:
        # Bybit v5 endpoint для історії прибутку/збитку закритих позицій
        # Отримуємо до 100 останніх закритих угод
        resp = exchange.privateGetV5PositionClosedPnl({
            'category': 'linear',
            'limit': 100
        })
        if resp.get('retCode') == 0:
            closed_pnl_records = resp.get('result', {}).get('list', [])
            print(f"✅ Успішно отримано {len(closed_pnl_records)} записів закритих позицій!")
        else:
            print(f"⚠️ Помилка Bybit API: {resp.get('retMsg')} (code: {resp.get('retCode')})")
    except Exception as e_pnl:
        print(f"⚠️ Помилка виклику privateGetV5PositionClosedPnl: {e_pnl}")

    # 5. Витягуємо історію виконаних ордерів (Executions / Trades)
    print("⏳ Отримання історії виконаних ордерів (Executions)...")
    executions = []
    try:
        resp_exec = exchange.privateGetV5ExecutionList({
            'category': 'linear',
            'limit': 100
        })
        if resp_exec.get('retCode') == 0:
            executions = resp_exec.get('result', {}).get('list', [])
            print(f"✅ Успішно отримано {len(executions)} записів виконаних угод!")
        else:
            print(f"⚠️ Помилка ExecutionList: {resp_exec.get('retMsg')}")
    except Exception as e_exec:
        print(f"⚠️ Помилка виклику ExecutionList: {e_exec}")

    # 6. Формуємо зведену таблицю та статистику
    if not closed_pnl_records and not executions:
        print("\nℹ️ На біржі ще немає історії виконаних угод для цього ключа.")
        return

    # Зберігаємо Closed PnL у CSV
    csv_pnl_path = os.path.join(_data_dir, "bybit_closed_pnl.csv")
    csv_fields = [
        "updated_time", "symbol", "side", "qty", "entry_price", "exit_price",
        "closed_pnl", "exec_type", "order_id"
    ]

    total_closed_pnl = 0.0
    wins_count = 0
    losses_count = 0
    best_trade = None
    worst_trade = None

    table_rows = []

    for r in closed_pnl_records:
        pnl = float(r.get("closedPnl", 0.0))
        total_closed_pnl += pnl
        if pnl > 0:
            wins_count += 1
        elif pnl < 0:
            losses_count += 1

        ts = int(r.get("updatedTime", 0)) / 1000.0
        time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts > 0 else "N/A"
        sym = r.get("symbol", "")
        side = r.get("side", "")
        qty = r.get("qty", "")
        entry = float(r.get("avgEntryPrice", 0.0) or r.get("entryPrice", 0.0) or 0.0)
        exit_p = float(r.get("avgExitPrice", 0.0) or 0.0)

        item = {
            "updated_time": time_str,
            "symbol": sym,
            "side": side,
            "qty": qty,
            "entry_price": entry,
            "exit_price": exit_p,
            "closed_pnl": round(pnl, 2),
            "exec_type": r.get("execType", ""),
            "order_id": r.get("orderId", "")
        }
        table_rows.append(item)

        if best_trade is None or pnl > best_trade["closed_pnl"]:
            best_trade = item
        if worst_trade is None or pnl < worst_trade["closed_pnl"]:
            worst_trade = item

    # Запис у CSV
    try:
        with open(csv_pnl_path, "w", encoding="utf-8", newline="") as cf:
            writer = csv.DictWriter(cf, fieldnames=csv_fields)
            writer.writeheader()
            for row in table_rows:
                writer.writerow(row)
        print(f"\n💾 Історію PnL збережено у: {csv_pnl_path}")
    except Exception as e_csv:
        print(f"⚠️ Помилка запису CSV: {e_csv}")

    # Також експорт у JSON
    json_path = os.path.join(_data_dir, "bybit_closed_pnl.json")
    try:
        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump({
                "exported_at": datetime.now().isoformat(),
                "total_closed_pnl": round(total_closed_pnl, 2),
                "wins": wins_count,
                "losses": losses_count,
                "trades": table_rows
            }, jf, indent=2)
        print(f"💾 JSON звіт збережено у: {json_path}")
    except Exception as e_json:
        print(f"⚠️ Помилка запису JSON: {e_json}")

    # 7. Виводимо таблицю у консоль
    print("\n" + "=" * 90)
    print("📋 ОСТАННІ 20 ЗАКРИТИХ ПОЗИЦІЙ НА BYBIT DEMO:")
    print("=" * 90)
    print(f"{'Дата/Час':<17} | {'Символ':<12} | {'Тип':<6} | {'Вхід':<10} | {'Вихід':<10} | {'PnL (USDT)':<12} | {'Результат'}")
    print("-" * 90)

    for row in table_rows[:20]:
        pnl_val = row['closed_pnl']
        res_str = f"🟢 WIN (+{pnl_val:.2f}$)" if pnl_val > 0 else (f"🔴 LOSS ({pnl_val:.2f}$)" if pnl_val < 0 else "⚪ BE (0.00$)")
        print(f"{row['updated_time']:<17} | {row['symbol']:<12} | {row['side']:<6} | {row['entry_price']:<10.4f} | {row['exit_price']:<10.4f} | {pnl_val:<12.2f} | {res_str}")

    print("=" * 90)
    print("📊 ПІДСУМКОВА СТАТИСТИКА БІРЖІ (BYBIT DEMO):")
    print("=" * 90)
    total_decided = wins_count + losses_count
    wr = (wins_count / total_decided * 100.0) if total_decided > 0 else 0.0
    print(f"• Всього закритих угод: {len(closed_pnl_records)}")
    print(f"• Прибуткових (WIN):     {wins_count} ({wr:.1f}%)")
    print(f"• Збиткових (LOSS):      {losses_count}")
    print(f"• Загальний PnL на біржі: {'+' if total_closed_pnl >= 0 else ''}{total_closed_pnl:,.2f} USDT")
    if best_trade:
        print(f"• Найкраща угода:        {best_trade['symbol']} ({best_trade['side']}) +{best_trade['closed_pnl']:.2f} USDT ({best_trade['updated_time']})")
    if worst_trade:
        print(f"• Найгірша угода:        {worst_trade['symbol']} ({worst_trade['side']}) {worst_trade['closed_pnl']:.2f} USDT ({worst_trade['updated_time']})")
    print("=" * 90)

if __name__ == "__main__":
    export_trades()
