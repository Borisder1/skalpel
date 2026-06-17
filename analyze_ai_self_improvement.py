#!/usr/bin/env python3
"""
🔬 AI Self-Improvement & Adaptation Audit
========================================
Analyse the database and configurations to report how the bot's AI has adapted:
1. Evolution of weights (Quant Engine weights over time from ai_memory).
2. Quant Score brackets performance (does a higher score mean higher win rate?).
3. Predictive power of individual factors (average value in WINS vs LOSSES).
4. Impact of Vision AI chart confirmations.
5. Progressive Blacklist events and active bans.
6. Cooperative Agents consensus configurations tuning.

Run on Render:
  python analyze_ai_self_improvement.py
"""

import os
import json
import sqlite3
from datetime import datetime
from collections import defaultdict

# DB and weight file paths
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history.db")
WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quant_weights.json")

DEFAULT_WEIGHTS = {
    "rr_quality":       0.18,
    "volume_confirm":   0.12,
    "adx_strength":     0.12,
    "fvg_size":         0.08,
    "htf_confluence":   0.12,
    "session_quality":  0.08,
    "smc_structure":    0.08,
    "impulse_quality":  0.04,
    "macro_confluence": 0.18,
    "news_sentiment":   0.05,
}

FACTOR_LABELS = {
    "rr_quality":       "Risk:Reward Quality",
    "volume_confirm":   "Volume Confirmation",
    "adx_strength":     "ADX Trend Strength",
    "fvg_size":         "FVG Size",
    "htf_confluence":   "HTF Confluence",
    "session_quality":  "Session Quality",
    "smc_structure":    "SMC Structure",
    "impulse_quality":  "Impulse Quality",
    "macro_confluence": "Macro Confluence",
    "news_sentiment":   "News Sentiment",
    "vision_score":     "Vision AI Confirmation"
}

def print_header(title):
    print("\n" + "=" * 80)
    print(f" {title} ".center(80, "="))
    print("=" * 80)

def load_current_weights():
    # Try loading from WEIGHTS_FILE
    if os.path.exists(WEIGHTS_FILE):
        try:
            with open(WEIGHTS_FILE, "r") as f:
                data = json.load(f)
                return data.get("weights", DEFAULT_WEIGHTS), "file (quant_weights.json)"
        except Exception:
            pass
            
    # Try loading from DB ai_memory
    if os.path.exists(DB_PATH):
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT best_weights_json FROM ai_memory ORDER BY id DESC LIMIT 1").fetchone()
                if row and row[0]:
                    return json.loads(row[0]), "database (ai_memory)"
        except Exception:
            pass
            
    return DEFAULT_WEIGHTS.copy(), "defaults"

def analyze_weights_evolution():
    print_header("1. QUANT ENGINE WEIGHTS EVOLUTION")
    current, source = load_current_weights()
    print(f"Loaded current weights from: {source}\n")
    
    print(f"{'Factor (Code)':<25} | {'Default':<7} | {'Current':<7} | {'Diff':<8} | {'Importance'}")
    print("-" * 80)
    
    # Sort factors by current weight
    sorted_factors = sorted(current.items(), key=lambda x: x[1], reverse=True)
    
    for k, curr_w in sorted_factors:
        def_w = DEFAULT_WEIGHTS.get(k, 0.0)
        diff = curr_w - def_w
        diff_str = f"{diff:+.2%}" if def_w > 0 else "NEW"
        stars = "*" * int(curr_w * 50)
        label = FACTOR_LABELS.get(k, k)
        print(f"{label:<25} | {def_w:<7.2f} | {curr_w:<7.2f} | {diff_str:<8} | {stars}")

def analyze_ai_memory_history():
    print_header("2. AI ADAPTATION HISTORY (ai_memory)")
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database trades_history.db not found.")
        return
        
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, timestamp, event_type, market_regime, simulated_pnl, report FROM ai_memory ORDER BY id DESC LIMIT 15"
            ).fetchall()
    except sqlite3.OperationalError:
        print("[INFO] Table ai_memory does not exist yet. It will be created when the bot starts.")
        return
        
    if not rows:
        print("[INFO] No records found in ai_memory table. Wait for the bot to run genetic evolution or weight learning.")
        return
        
    print(f"{'ID':<3} | {'Timestamp':<19} | {'Event Type':<18} | {'Regime':<10} | {'Sim PnL':<8} | {'Report Summary'}")
    print("-" * 100)
    for row in rows:
        report_text = row["report"] or ""
        # Clean up report text for single line summary
        report_summary = report_text.replace("\n", " ").strip()
        if len(report_summary) > 45:
            report_summary = report_summary[:42] + "..."
            
        sim_pnl_str = f"{row['simulated_pnl']:+.2f}" if row["simulated_pnl"] is not None else "N/A"
        print(f"{row['id']:<3} | {row['timestamp']:<19} | {row['event_type']:<18} | {row['market_regime']:<10} | {sim_pnl_str:<8} | {report_summary}")

def analyze_score_brackets():
    print_header("3. QUANT SCORE PERFORMANCE BRACKETS")
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database trades_history.db not found.")
        return
        
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute(
                "SELECT quant_score, status, pnl FROM trades WHERE status IN ('WIN', 'LOSS', 'VIRTUAL_WIN', 'VIRTUAL_LOSS')"
            ).fetchall()
    except sqlite3.OperationalError:
        print("[INFO] Table trades does not exist yet. It will be created when the bot starts.")
        return
        
    if not trades:
        print("[INFO] No closed trades (WIN/LOSS/VIRTUAL_WIN/VIRTUAL_LOSS) found in DB yet.")
        return
        
    # Define brackets
    brackets = [
        {"name": "Ultra Conf (>0.85)", "min": 0.85, "max": 1.01, "trades": []},
        {"name": "High Conf (0.75-0.85)", "min": 0.75, "max": 0.85, "trades": []},
        {"name": "Auto Exec (0.65-0.75)", "min": 0.65, "max": 0.75, "trades": []},
        {"name": "Manual Conf (0.40-0.65)", "min": 0.40, "max": 0.65, "trades": []},
        {"name": "Low Conf (<0.40)", "min": 0.0, "max": 0.40, "trades": []},
    ]
    
    for t in trades:
        score = t["quant_score"] or 0.0
        pnl = t["pnl"] or 0.0
        status = t["status"]
        
        for b in brackets:
            if b["min"] <= score < b["max"]:
                b["trades"].append({"pnl": pnl, "status": status})
                break
                
    print(f"{'Bracket':<25} | {'Total':<6} | {'Wins':<5} | {'Losses':<6} | {'Win Rate':<8} | {'Net PnL':<10} | {'Profit Factor'}")
    print("-" * 85)
    
    for b in brackets:
        total = len(b["trades"])
        if total == 0:
            print(f"{b['name']:<25} | {0:<6} | {0:<5} | {0:<6} | {'0.0%':<8} | {'0.00':<10} | {'0.00'}")
            continue
            
        wins = sum(1 for x in b["trades"] if "WIN" in x["status"])
        losses = sum(1 for x in b["trades"] if "LOSS" in x["status"])
        net_pnl = sum(x["pnl"] for x in b["trades"])
        
        gross_profit = sum(x["pnl"] for x in b["trades"] if x["pnl"] > 0)
        gross_loss = sum(abs(x["pnl"]) for x in b["trades"] if x["pnl"] < 0)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 1.0)
        pf_str = f"{profit_factor:.2f}" if profit_factor != float('inf') else "inf"
        
        win_rate = (wins / total) if total > 0 else 0.0
        print(f"{b['name']:<25} | {total:<6} | {wins:<5} | {losses:<6} | {win_rate:<8.1%} | {net_pnl:<+10.2f} | {pf_str}")

def analyze_factor_correlations():
    print_header("4. INDIVIDUAL FACTOR PREDICTIVE POWER")
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database trades_history.db not found.")
        return
        
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute(
                "SELECT factors_snapshot, status FROM trades WHERE status IN ('WIN', 'LOSS', 'VIRTUAL_WIN', 'VIRTUAL_LOSS')"
            ).fetchall()
    except sqlite3.OperationalError:
        return
        
    if not trades:
        print("[INFO] No closed trades in DB to analyze factors.")
        return
        
    win_factors = defaultdict(list)
    loss_factors = defaultdict(list)
    
    win_count = 0
    loss_count = 0
    
    for t in trades:
        status = t["status"]
        factors_str = t["factors_snapshot"]
        if not factors_str:
            continue
            
        try:
            factors = json.loads(factors_str)
            if not factors:
                continue
        except Exception:
            continue
            
        is_win = "WIN" in status
        if is_win:
            win_count += 1
            for k, v in factors.items():
                win_factors[k].append(v)
        else:
            loss_count += 1
            for k, v in factors.items():
                loss_factors[k].append(v)
                
    if win_count == 0 or loss_count == 0:
        print(f"[INFO] Need both wins and losses to calculate factor predictive power. Wins: {win_count}, Losses: {loss_count}")
        return
        
    print(f"Analyzing {win_count} Winning trades vs {loss_count} Losing trades.\n")
    print(f"{'Factor Description':<25} | {'Avg in Wins':<12} | {'Avg in Losses':<13} | {'Difference':<11} | {'Correlation / Predictive Power'}")
    print("-" * 90)
    
    all_keys = set(list(win_factors.keys()) + list(loss_factors.keys()))
    sorted_keys = sorted(all_keys)
    
    for k in sorted_keys:
        avg_win = sum(win_factors[k]) / len(win_factors[k]) if win_factors[k] else 0.0
        avg_loss = sum(loss_factors[k]) / len(loss_factors[k]) if loss_factors[k] else 0.0
        diff = avg_win - avg_loss
        
        # Predictive power assessment
        if diff > 0.05:
            assessment = "[GOOD] STRONG CONFIRMER (Better in wins)"
        elif diff > 0.01:
            assessment = "[GOOD] WEAK CONFIRMER"
        elif diff < -0.05:
            assessment = "[WARN] CONTRARIAN / WARNING (Higher in losses)"
        elif diff < -0.01:
            assessment = "[WARN] WEAK WARNING"
        else:
            assessment = "[NEUT] NEUTRAL / NOISY"
            
        label = FACTOR_LABELS.get(k, k)
        print(f"{label:<25} | {avg_win:<12.2%} | {avg_loss:<13.2%} | {diff:<+11.2%} | {assessment}")

def analyze_vision_impact():
    print_header("5. VISION AI CONFIRMATION PERFORMANCE")
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database trades_history.db not found.")
        return
        
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute(
                "SELECT factors_snapshot, status, pnl FROM trades WHERE status IN ('WIN', 'LOSS', 'VIRTUAL_WIN', 'VIRTUAL_LOSS')"
            ).fetchall()
    except sqlite3.OperationalError:
        return
        
    if not trades:
        print("[INFO] No closed trades in DB.")
        return
        
    vision_confirmed = []
    vision_not_confirmed = []
    
    for t in trades:
        pnl = t["pnl"] or 0.0
        status = t["status"]
        factors_str = t["factors_snapshot"]
        has_vision_bonus = False
        
        if factors_str:
            try:
                factors = json.loads(factors_str)
                # Check if vision_score exists and was a positive bonus
                if factors.get("vision_score", 0.0) > 0.0:
                    has_vision_bonus = True
            except:
                pass
                
        trade_data = {"pnl": pnl, "status": status}
        if has_vision_bonus:
            vision_confirmed.append(trade_data)
        else:
            vision_not_confirmed.append(trade_data)
            
    print(f"{'Group':<25} | {'Total':<6} | {'Wins':<5} | {'Losses':<6} | {'Win Rate':<8} | {'Net PnL':<10} | {'Profit Factor'}")
    print("-" * 85)
    
    for group_name, group_data in [("Vision AI Confirmed", vision_confirmed), ("No Vision AI Bonus", vision_not_confirmed)]:
        total = len(group_data)
        if total == 0:
            print(f"{group_name:<25} | {0:<6} | {0:<5} | {0:<6} | {'0.0%':<8} | {'0.00':<10} | {'0.00'}")
            continue
            
        wins = sum(1 for x in group_data if "WIN" in x["status"])
        losses = sum(1 for x in group_data if "LOSS" in x["status"])
        net_pnl = sum(x["pnl"] for x in group_data)
        
        gross_profit = sum(x["pnl"] for x in group_data if x["pnl"] > 0)
        gross_loss = sum(abs(x["pnl"]) for x in group_data if x["pnl"] < 0)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 1.0)
        pf_str = f"{profit_factor:.2f}" if profit_factor != float('inf') else "inf"
        
        win_rate = (wins / total) if total > 0 else 0.0
        print(f"{group_name:<25} | {total:<6} | {wins:<5} | {losses:<6} | {win_rate:<8.1%} | {net_pnl:<+10.2f} | {pf_str}")

def analyze_blacklist():
    print_header("6. PROGRESSIVE BLACKLIST STATUS")
    if not os.path.exists(DB_PATH):
        print("[ERROR] Database trades_history.db not found.")
        return
        
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            # Active blacklisted items
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            active_bans = conn.execute(
                "SELECT symbol, reason, blacklisted_at, expires_at, loss_count FROM symbol_blacklist WHERE expires_at > ? ORDER BY expires_at ASC",
                (now_str,)
            ).fetchall()
            
            # Expired blacklisted items
            expired_bans = conn.execute(
                "SELECT symbol, reason, expires_at, loss_count FROM symbol_blacklist WHERE expires_at <= ? ORDER BY expires_at DESC LIMIT 10",
                (now_str,)
            ).fetchall()
    except sqlite3.OperationalError:
        print("[INFO] Table symbol_blacklist does not exist yet. It will be created when the bot starts.")
        return
        
    print("Active bans (currently blocked from trading):")
    if not active_bans:
        print("  [OK] No active bans. All coins are tradable.")
    else:
        print(f"  {'Symbol':<18} | {'Banned At':<19} | {'Expires At':<19} | {'Level':<5} | {'Reason'}")
        print("  " + "-" * 85)
        for b in active_bans:
            print(f"  {b['symbol']:<18} | {b['blacklisted_at']:<19} | {b['expires_at']:<19} | {b['loss_count']:<5} | {b['reason']}")
            
    print("\nRecent expired bans:")
    if not expired_bans:
        print("  No previous bans recorded.")
    else:
        print(f"  {'Symbol':<18} | {'Expired At':<19} | {'Bans count':<10} | {'Last Reason'}")
        print("  " + "-" * 70)
        for b in expired_bans:
            print(f"  {b['symbol']:<18} | {b['expires_at']:<19} | {b['loss_count']:<10} | {b['reason']}")

def main():
    print_header("AI ADAPTATION & SELF-IMPROVEMENT REPORT")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database: {DB_PATH}")
    
    analyze_weights_evolution()
    analyze_ai_memory_history()
    analyze_score_brackets()
    analyze_factor_correlations()
    analyze_vision_impact()
    analyze_blacklist()
    
if __name__ == "__main__":
    main()
