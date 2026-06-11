import json
import os
from datetime import datetime

STATS_FILE = "trading_stats.json"


def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "trades": [],
    }


def record_trade(symbol, direction, entry, exit_price, pnl):
    stats = load_stats()
    stats["total_trades"] += 1
    stats["total_pnl"] += float(pnl)
    if pnl > 0:
        stats["wins"] += 1
        stats["best_trade"] = max(stats["best_trade"], float(pnl))
    else:
        stats["losses"] += 1
        stats["worst_trade"] = min(stats["worst_trade"], float(pnl))

    stats["trades"].append(
        {
            "time": datetime.now().isoformat(),
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "exit": exit_price,
            "pnl": pnl,
        }
    )

    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    return stats


def get_summary():
    s = load_stats()
    if s["total_trades"] == 0:
        return "Угод ще немає"
    wr = s["wins"] / s["total_trades"] * 100
    pnl_sign = "+" if s["total_pnl"] > 0 else ""
    return (
        f"📈 *Статистика торгівлі*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Угод: *{s['total_trades']}* ✅ {s['wins']} / ❌ {s['losses']}\n"
        f"Win Rate: *{wr:.1f}%*\n"
        f"Загальний PnL: *{pnl_sign}{s['total_pnl']:.4f} USDT*\n"
        f"Краща угода: *+{s['best_trade']:.4f}*\n"
        f"Гірша угода: *{s['worst_trade']:.4f}*\n"
        f"━━━━━━━━━━━━━━━"
    )

def get_consecutive_losses():
    s = load_stats()
    trades = s.get("trades", [])
    losses = 0
    for t in reversed(trades):
        if t.get("pnl", 0) < 0:
            losses += 1
        else:
            break
    return losses
    
def get_recent_trades_context(limit=3):
    s = load_stats()
    trades = s.get("trades", [])[-limit:]
    context = ""
    for i, t in enumerate(trades):
        res = "LOSS" if t.get("pnl", 0) < 0 else "WIN"
        context += f"Trade {i+1}: {t.get('direction')} {t.get('symbol')}. Entry: {t.get('entry')}, Exit: {t.get('exit')}. Result: {res} ({t.get('pnl')} USDT).\n"
    return context
