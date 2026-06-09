"""
📊 Bybit Demo PnL Audit Script
Витягує закриті позиції з Bybit Demo API та аналізує продуктивність.
Також аналізує локальну БД trades_history.db.

Запуск на Render сервері:
  python audit_pnl.py

Або локально (якщо є .env з BYBIT_API_KEY / BYBIT_API_SECRET):
  python audit_pnl.py
"""

import os
import sys
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

try:
    import ccxt
except ImportError:
    print("❌ ccxt не встановлено. pip install ccxt")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ──────────────────────────────────────────────
# 1. ІНІЦІАЛІЗАЦІЯ EXCHANGE
# ──────────────────────────────────────────────
def init_exchange():
    """Ініціалізація з'єднання з Bybit Demo."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_config.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

    api_key = config.get("api_key") or os.getenv("BYBIT_API_KEY", "")
    api_secret = config.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
    base_url = config.get("base_url", "https://api-demo.bybit.com")

    if not api_key or not api_secret:
        print("❌ API ключі не знайдено (ні в active_config.json, ні в ENV)")
        print("   Потрібно: BYBIT_API_KEY та BYBIT_API_SECRET")
        return None

    exchange = ccxt.bybit({
        "enableRateLimit": True,
        "apiKey": api_key,
        "secret": api_secret,
        "urls": {"api": base_url},
        "options": {"defaultType": "future", "recvWindow": 10000},
    })

    try:
        exchange.enableDemoTrading(True)
    except Exception:
        pass

    # Тест підключення
    try:
        bal = exchange.fetch_balance()
        usdt = (bal.get("USDT") or {}).get("total", "N/A")
        print(f"✅ Підключено до Bybit Demo | USDT balance: {usdt}")
    except Exception as e:
        print(f"❌ Помилка підключення: {e}")
        return None

    return exchange


# ──────────────────────────────────────────────
# 2. ВИТЯГУВАННЯ ЗАКРИТИХ PnL З BYBIT API
# ──────────────────────────────────────────────
def fetch_all_closed_pnl(exchange, days_back=30, category="linear"):
    """
    Витягує ВСІ закриті PnL за останні N днів.
    Bybit V5 API: /v5/position/closed-pnl (max 100 per page, with cursor pagination).
    """
    all_records = []
    cursor = ""
    start_time = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000)
    page = 0
    max_pages = 50  # Безпечний ліміт

    while page < max_pages:
        params = {
            "category": category,
            "limit": 100,
            "startTime": start_time,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            response = exchange.private_get_v5_position_closed_pnl(params)
        except Exception as e:
            print(f"⚠️ Помилка запиту closed PnL (page {page}): {e}")
            break

        if not isinstance(response, dict):
            break

        ret_code = response.get("retCode")
        if ret_code != 0:
            print(f"⚠️ API retCode={ret_code}: {response.get('retMsg', 'unknown')}")
            break

        result = response.get("result", {})
        records = result.get("list", [])
        if not records:
            break

        all_records.extend(records)
        page += 1

        next_cursor = result.get("nextPageCursor", "")
        if not next_cursor:
            break
        cursor = next_cursor

        time.sleep(0.3)  # Антирейтліміт

    print(f"📄 Витягнуто {len(all_records)} записів closed PnL з Bybit API (за {days_back} днів)")
    return all_records


# ──────────────────────────────────────────────
# 3. ВИТЯГУВАННЯ ЗАКРИТИХ ОРДЕРІВ З BYBIT API
# ──────────────────────────────────────────────
def fetch_all_closed_orders(exchange, days_back=30):
    """
    Витягує закриті ордери через fetchClosedOrders (CCXT unified).
    """
    all_orders = []
    since = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000)

    try:
        orders = exchange.fetch_closed_orders(symbol=None, since=since, limit=200, params={"category": "linear"})
        all_orders.extend(orders)
        print(f"📄 Витягнуто {len(all_orders)} закритих ордерів з CCXT")
    except Exception as e:
        print(f"⚠️ fetchClosedOrders не підтримується або помилка: {e}")
        # Fallback: спробуємо через нативний API
        try:
            params = {
                "category": "linear",
                "limit": 50,
                "orderStatus": "Filled",
            }
            response = exchange.private_get_v5_order_history(params)
            if response.get("retCode") == 0:
                records = response.get("result", {}).get("list", [])
                all_orders = records
                print(f"📄 Витягнуто {len(records)} ордерів через V5 order history")
        except Exception as e2:
            print(f"⚠️ Fallback order history також не працює: {e2}")

    return all_orders


# ──────────────────────────────────────────────
# 4. АНАЛІЗ ЗАКРИТИХ PnL
# ──────────────────────────────────────────────
def analyze_closed_pnl(records):
    """Аналізує записи closed PnL."""
    if not records:
        print("\n⚠️ Немає записів для аналізу\n")
        return

    wins = 0
    losses = 0
    breakeven = 0
    total_pnl = 0.0
    total_win_pnl = 0.0
    total_loss_pnl = 0.0
    best_trade = {"pnl": -float("inf"), "symbol": "N/A"}
    worst_trade = {"pnl": float("inf"), "symbol": "N/A"}
    trades_by_symbol = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0})
    trades_by_side = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0})
    trades_by_day = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0})
    durations = []

    for r in records:
        pnl = float(r.get("closedPnl", 0))
        symbol = r.get("symbol", "UNKNOWN")
        side = r.get("side", "Unknown")
        qty = float(r.get("qty", 0))
        avg_entry = float(r.get("avgEntryPrice", 0))
        avg_exit = float(r.get("avgExitPrice", 0))
        created_ts = int(r.get("createdTime", 0))
        updated_ts = int(r.get("updatedTime", 0))

        # Тривалість позиції
        if created_ts > 0 and updated_ts > 0 and updated_ts > created_ts:
            duration_min = (updated_ts - created_ts) / 60000
            durations.append(duration_min)

        total_pnl += pnl

        # День
        if created_ts > 0:
            day = datetime.fromtimestamp(created_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            day = "unknown"

        trades_by_symbol[symbol]["count"] += 1
        trades_by_symbol[symbol]["pnl"] += pnl
        trades_by_side[side]["count"] += 1
        trades_by_side[side]["pnl"] += pnl
        trades_by_day[day]["count"] += 1
        trades_by_day[day]["pnl"] += pnl

        if pnl > 0.001:
            wins += 1
            total_win_pnl += pnl
            trades_by_symbol[symbol]["wins"] += 1
            trades_by_side[side]["wins"] += 1
            trades_by_day[day]["wins"] += 1
        elif pnl < -0.001:
            losses += 1
            total_loss_pnl += pnl
            trades_by_symbol[symbol]["losses"] += 1
            trades_by_side[side]["losses"] += 1
            trades_by_day[day]["losses"] += 1
        else:
            breakeven += 1

        if pnl > best_trade["pnl"]:
            best_trade = {"pnl": pnl, "symbol": symbol, "side": side, "entry": avg_entry, "exit": avg_exit, "qty": qty}
        if pnl < worst_trade["pnl"]:
            worst_trade = {"pnl": pnl, "symbol": symbol, "side": side, "entry": avg_entry, "exit": avg_exit, "qty": qty}

    total = wins + losses + breakeven
    win_rate = (wins / max(total, 1)) * 100
    profit_factor = abs(total_win_pnl / min(total_loss_pnl, -0.001)) if total_loss_pnl < 0 else float("inf")
    avg_win = total_win_pnl / max(wins, 1)
    avg_loss = total_loss_pnl / max(losses, 1)
    avg_duration = sum(durations) / max(len(durations), 1) if durations else 0

    # ──────── ГОЛОВНИЙ ЗВІТ ────────
    print("\n" + "═" * 60)
    print("📊 АУДИТ ЗАКРИТИХ ПОЗИЦІЙ — BYBIT DEMO")
    print("═" * 60)
    print(f"  Всього угод:        {total}")
    print(f"  ✅ Прибуткових:      {wins} ({win_rate:.1f}%)")
    print(f"  ❌ Збиткових:        {losses} ({100 - win_rate:.1f}%)")
    print(f"  ➖ Беззбиткових:     {breakeven}")
    print(f"  {'─' * 40}")
    print(f"  💰 Загальний PnL:    {'+' if total_pnl >= 0 else ''}{total_pnl:.4f} USDT")
    print(f"  📈 Прибуток (сума):  +{total_win_pnl:.4f} USDT")
    print(f"  📉 Збиток (сума):    {total_loss_pnl:.4f} USDT")
    print(f"  {'─' * 40}")
    print(f"  🏆 Profit Factor:    {profit_factor:.2f}")
    print(f"  📊 Середній WIN:     +{avg_win:.4f} USDT")
    print(f"  📉 Середній LOSS:    {avg_loss:.4f} USDT")
    print(f"  ⏱️ Сер. тривалість:  {avg_duration:.0f} хв ({avg_duration/60:.1f} год)")

    # Краща/гірша угода
    print(f"\n  🏅 Краща угода:      {best_trade['symbol']} ({best_trade.get('side','?')}) → +{best_trade['pnl']:.4f} USDT")
    print(f"  💀 Гірша угода:      {worst_trade['symbol']} ({worst_trade.get('side','?')}) → {worst_trade['pnl']:.4f} USDT")

    # По символах
    print(f"\n{'─' * 60}")
    print("📋 ДЕТАЛІЗАЦІЯ ПО СИМВОЛАХ:")
    print(f"{'─' * 60}")
    print(f"  {'Символ':<20} {'Угод':>5} {'Win':>4} {'Loss':>5} {'WR%':>6} {'PnL':>12}")
    print(f"  {'─' * 58}")
    for sym, data in sorted(trades_by_symbol.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = (data["wins"] / max(data["count"], 1)) * 100
        pnl_str = f"{'+' if data['pnl'] >= 0 else ''}{data['pnl']:.4f}"
        print(f"  {sym:<20} {data['count']:>5} {data['wins']:>4} {data['losses']:>5} {wr:>5.1f}% {pnl_str:>12}")

    # По напрямках
    print(f"\n{'─' * 60}")
    print("📋 ДЕТАЛІЗАЦІЯ ПО НАПРЯМКАХ (LONG/SHORT):")
    print(f"{'─' * 60}")
    for side, data in trades_by_side.items():
        wr = (data["wins"] / max(data["count"], 1)) * 100
        pnl_str = f"{'+' if data['pnl'] >= 0 else ''}{data['pnl']:.4f}"
        print(f"  {side:<10} | Угод: {data['count']:>3} | Win: {data['wins']:>3} | Loss: {data['losses']:>3} | WR: {wr:.1f}% | PnL: {pnl_str}")

    # По днях
    print(f"\n{'─' * 60}")
    print("📋 ДЕТАЛІЗАЦІЯ ПО ДНЯХ:")
    print(f"{'─' * 60}")
    for day in sorted(trades_by_day.keys()):
        data = trades_by_day[day]
        wr = (data["wins"] / max(data["count"], 1)) * 100
        pnl_str = f"{'+' if data['pnl'] >= 0 else ''}{data['pnl']:.4f}"
        emoji = "🟢" if data["pnl"] >= 0 else "🔴"
        print(f"  {emoji} {day} | Угод: {data['count']:>3} | Win: {data['wins']:>3} | Loss: {data['losses']:>3} | WR: {wr:.1f}% | PnL: {pnl_str}")

    # Висновок
    print(f"\n{'═' * 60}")
    if win_rate >= 60:
        print(f"  ✅ СТРАТЕГІЯ ПРАЦЮЄ: Win Rate {win_rate:.1f}% >= 60%")
    elif win_rate >= 50:
        print(f"  ⚠️ СТРАТЕГІЯ ПОТРЕБУЄ ОПТИМІЗАЦІЇ: Win Rate {win_rate:.1f}% (50-60%)")
    else:
        print(f"  ❌ СТРАТЕГІЯ НЕ ПРАЦЮЄ: Win Rate {win_rate:.1f}% < 50%")

    if profit_factor >= 1.5:
        print(f"  ✅ Profit Factor {profit_factor:.2f} >= 1.5 (добре)")
    elif profit_factor >= 1.0:
        print(f"  ⚠️ Profit Factor {profit_factor:.2f} >= 1.0 (ще прибутково, але слабо)")
    else:
        print(f"  ❌ Profit Factor {profit_factor:.2f} < 1.0 (збитково)")
    print("═" * 60)

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


# ──────────────────────────────────────────────
# 5. АНАЛІЗ ЛОКАЛЬНОЇ БД
# ──────────────────────────────────────────────
def analyze_local_db():
    """Аналізує trades_history.db."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history.db")
    if not os.path.exists(db_path):
        print(f"\n⚠️ Локальна БД не знайдена: {db_path}\n")
        return

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Всі записи
        cur.execute("SELECT * FROM trades ORDER BY timestamp DESC")
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("\n⚠️ Локальна БД порожня\n")
        return

    print(f"\n{'═' * 60}")
    print(f"🗃️  АНАЛІЗ ЛОКАЛЬНОЇ БД (trades_history.db)")
    print(f"{'═' * 60}")
    print(f"  Записів: {len(rows)}")

    by_status = defaultdict(int)
    total_pnl = 0.0
    for r in rows:
        status = r.get("status", "UNKNOWN")
        by_status[status] += 1
        total_pnl += float(r.get("pnl", 0) or 0)

    for status, count in sorted(by_status.items()):
        print(f"  {status}: {count}")
    print(f"  Загальний PnL (з БД): {total_pnl:.4f} USDT")

    print(f"\n  {'ID':>4} {'Дата':<20} {'Символ':<20} {'Напр':<6} {'Статус':<10} {'PnL':>12} {'OrderID':<20}")
    print(f"  {'─' * 96}")
    for r in rows:
        pnl = float(r.get("pnl", 0) or 0)
        pnl_str = f"{'+' if pnl > 0 else ''}{pnl:.4f}" if pnl != 0 else "0.0000"
        order_id = (r.get("order_id") or "N/A")[:18]
        print(f"  {r['id']:>4} {r.get('timestamp', 'N/A'):<20} {r.get('symbol', 'N/A'):<20} {r.get('direction', '?'):<6} {r.get('status', '?'):<10} {pnl_str:>12} {order_id:<20}")
    print(f"{'═' * 60}\n")


# ──────────────────────────────────────────────
# 6. СПИСОК ВІДКРИТИХ ПОЗИЦІЙ/ОРДЕРІВ
# ──────────────────────────────────────────────
def show_current_state(exchange):
    """Показує поточні відкриті позиції та ордери."""
    print(f"\n{'═' * 60}")
    print("📋 ПОТОЧНИЙ СТАН НА BYBIT DEMO")
    print(f"{'═' * 60}")

    # Позиції
    try:
        positions = exchange.fetch_positions()
        active = [p for p in positions if float(p.get("contracts", 0) or 0) != 0]
        if active:
            print(f"\n  🔓 Відкриті позиції ({len(active)}):")
            for p in active:
                side = p.get("side", "?")
                sym = p.get("symbol", "?")
                contracts = p.get("contracts", 0)
                entry = p.get("entryPrice", 0)
                unrealized = float(p.get("unrealizedPnl", 0) or 0)
                liq_price = p.get("liquidationPrice", "N/A")
                emoji = "🟢" if unrealized >= 0 else "🔴"
                print(f"    {emoji} {sym:<20} {side:<6} qty={contracts} entry={entry} uPnL={unrealized:+.4f} liq={liq_price}")
        else:
            print("\n  ✅ Немає відкритих позицій")
    except Exception as e:
        print(f"  ⚠️ Помилка отримання позицій: {e}")

    # Ордери
    try:
        orders = exchange.fetch_open_orders()
        if orders:
            print(f"\n  📝 Відкриті ордери ({len(orders)}):")
            for o in orders:
                sym = o.get("symbol", "?")
                side = o.get("side", "?")
                price = o.get("price", 0)
                amount = o.get("amount", 0)
                status = o.get("status", "?")
                created = o.get("datetime", "?")
                print(f"    📌 {sym:<20} {side:<6} price={price} qty={amount} status={status} created={created}")
        else:
            print("\n  ✅ Немає відкритих ордерів")
    except Exception as e:
        print(f"  ⚠️ Помилка отримання ордерів: {e}")

    print(f"{'═' * 60}\n")


# ──────────────────────────────────────────────
# 7. ДЕТАЛІЗОВАНИЙ ВИВІД КОЖНОЇ УГОДИ
# ──────────────────────────────────────────────
def print_all_trades(records):
    """Друкує таблицю всіх закритих угод."""
    if not records:
        return

    print(f"\n{'═' * 100}")
    print("📜 ПОВНИЙ СПИСОК ЗАКРИТИХ УГОД (Bybit API)")
    print(f"{'═' * 100}")
    print(f"  {'#':>3} {'Дата':<20} {'Символ':<18} {'Side':<6} {'Entry':>12} {'Exit':>12} {'Qty':>10} {'PnL':>12} {'Статус'}")
    print(f"  {'─' * 95}")

    # Сортуємо за часом
    sorted_records = sorted(records, key=lambda x: int(x.get("createdTime", 0)))

    for idx, r in enumerate(sorted_records, 1):
        pnl = float(r.get("closedPnl", 0))
        symbol = r.get("symbol", "?")
        side = r.get("side", "?")
        entry = r.get("avgEntryPrice", "?")
        exit_p = r.get("avgExitPrice", "?")
        qty = r.get("qty", "?")
        ts = int(r.get("createdTime", 0))
        date_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts > 0 else "N/A"
        status = "✅ WIN" if pnl > 0.001 else ("❌ LOSS" if pnl < -0.001 else "➖ BE")
        pnl_str = f"{'+' if pnl > 0 else ''}{pnl:.4f}"
        print(f"  {idx:>3} {date_str:<20} {symbol:<18} {side:<6} {entry:>12} {exit_p:>12} {qty:>10} {pnl_str:>12} {status}")

    print(f"{'═' * 100}\n")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    print("\n" + "🔍" * 30)
    print("   BYBIT DEMO — ПОВНИЙ АУДИТ PnL")
    print("🔍" * 30 + "\n")

    # 1. Підключення
    exchange = init_exchange()

    # 2. Аналіз локальної БД (завжди)
    analyze_local_db()

    if exchange is None:
        print("⚠️ Без API-ключів доступний тільки аналіз локальної БД.")
        print("   Для повного аудиту встановіть BYBIT_API_KEY та BYBIT_API_SECRET.")
        return

    # 3. Поточний стан
    show_current_state(exchange)

    # 4. Витягування closed PnL
    days_back = 30
    if len(sys.argv) > 1:
        try:
            days_back = int(sys.argv[1])
        except ValueError:
            pass

    print(f"\n⏳ Витягуємо закриті PnL за останні {days_back} днів...\n")
    records = fetch_all_closed_pnl(exchange, days_back=days_back)

    # 5. Повний список угод
    print_all_trades(records)

    # 6. Аналіз
    result = analyze_closed_pnl(records)

    # 7. Також пробуємо order history
    print("\n⏳ Витягуємо історію ордерів...\n")
    orders = fetch_all_closed_orders(exchange, days_back=days_back)
    if orders:
        print(f"\n📋 Останні 10 закритих ордерів:")
        for o in orders[:10]:
            if isinstance(o, dict):
                sym = o.get("symbol") or o.get("info", {}).get("symbol", "?")
                side = o.get("side") or o.get("info", {}).get("side", "?")
                status = o.get("status") or o.get("info", {}).get("orderStatus", "?")
                price = o.get("price") or o.get("info", {}).get("price", "?")
                filled = o.get("filled") or o.get("info", {}).get("cumExecQty", "?")
                print(f"  {sym:<20} {side:<6} price={price} filled={filled} status={status}")

    print("\n✅ Аудит завершено.")


if __name__ == "__main__":
    main()
