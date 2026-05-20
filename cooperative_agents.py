import os
import json
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
from db_logger import DB_PATH
from racer_core import analyze_racer
from optimizer import optimize, RACER_FAST_GRID
from telegram_notifier import send_telegram_message

class PatternAnalyzerAgent:
    """
    Агент виявлення паттернів.
    Спеціалізується на виявленні графічних паттернів, FVG, Liquidity sweeps та оцінці їхньої частоти/успішності.
    """
    def __init__(self):
        self.name = "Pattern Analyzer Agent"

    def analyze_market_patterns(self, df: pd.DataFrame, htf_df: pd.DataFrame, config: dict) -> dict:
        """Аналізує свіжі графічні паттерни на ринку."""
        try:
            states = analyze_racer(df, htf_df, config)
            if not states:
                return {"bullish_setups": 0, "bearish_setups": 0, "recent_setup": None}
            
            recent_states = states[-50:] # Останні 50 барів
            bull_setups = sum(1 for s in recent_states if s.setup and s.setup.valid and s.setup.dir == 1)
            bear_setups = sum(1 for s in recent_states if s.setup and s.setup.valid and s.setup.dir == -1)
            
            last_setup = None
            for s in reversed(states):
                if s.setup and s.setup.valid:
                    last_setup = {
                        "direction": "LONG" if s.setup.dir == 1 else "SHORT",
                        "entry": s.setup.entry,
                        "sl": s.setup.sl,
                        "tp1": s.setup.tp1,
                        "tp2": s.setup.tp2,
                        "timestamp": str(s.timestamp)
                    }
                    break
            
            return {
                "bullish_setups": bull_setups,
                "bearish_setups": bear_setups,
                "recent_setup": last_setup,
                "total_analyzed_bars": len(states)
            }
        except Exception as e:
            print(f"[{self.name}] Помилка аналізу паттернів: {e}")
            return {"bullish_setups": 0, "bearish_setups": 0, "recent_setup": None}

    def get_pattern_success_rate(self) -> dict:
        """Оцінює успішність сигналів на основі реальної історії угод з БД."""
        if not os.path.exists(DB_PATH):
            return {"total_patterns": 0, "success_rate": 0.0}
        
        try:
            conn = sqlite3.connect(DB_PATH)
            df = pd.read_sql_query("SELECT status FROM trades WHERE status IN ('WIN', 'LOSS')", conn)
            conn.close()
            
            if df.empty:
                return {"total_patterns": 0, "success_rate": 0.0}
            
            wins = len(df[df["status"] == "WIN"])
            total = len(df)
            success_rate = (wins / total) * 100
            
            return {
                "total_patterns": total,
                "success_rate": round(success_rate, 2),
                "wins": wins,
                "losses": total - wins
            }
        except Exception as e:
            print(f"[{self.name}] Помилка зчитування статистики БД: {e}")
            return {"total_patterns": 0, "success_rate": 0.0}


class TradeDiagnosticsAgent:
    """
    Агент діагностики торгівлі та управління ризиком.
    Аналізує прибутки, збитки, просадки та запускає Grid Search для оптимізації параметрів.
    """
    def __init__(self):
        self.name = "Trade Diagnostics Agent"

    def run_diagnostics(self) -> dict:
        """Аналізує ефективність торгівлі та виявляє слабкі місця."""
        if not os.path.exists(DB_PATH):
            return {"status": "no_data", "msg": "База даних угод ще не створена."}
        
        try:
            conn = sqlite3.connect(DB_PATH)
            df = pd.read_sql_query("SELECT * FROM trades", conn)
            conn.close()
            
            if df.empty:
                return {"status": "no_data", "msg": "В базі даних ще немає записаних угод."}
            
            total_trades = len(df)
            completed_trades = df[df["status"].isin(["WIN", "LOSS"])]
            
            if completed_trades.empty:
                return {
                    "status": "collecting_data",
                    "total_trades": total_trades,
                    "msg": f"Зібрано {total_trades} угод. Очікуємо закриття для первинної діагностики."
                }
            
            wins = len(completed_trades[completed_trades["status"] == "WIN"])
            losses = len(completed_trades[completed_trades["status"] == "LOSS"])
            win_rate = (wins / len(completed_trades)) * 100
            
            total_pnl = completed_trades["pnl"].sum()
            profit_factor = 1.0
            gross_profit = completed_trades[completed_trades["pnl"] > 0]["pnl"].sum()
            gross_loss = abs(completed_trades[completed_trades["pnl"] < 0]["pnl"].sum())
            if gross_loss > 0:
                profit_factor = gross_profit / gross_loss
            
            # Діагностична порада
            advice = "Поточні параметри працюють стабільно."
            if win_rate < 40:
                advice = "Занадто низький відсоток виграшів. Рекомендується збільшити Fibonacci рівень або пом'якшити фільтр тренду ADX."
            elif profit_factor < 1.0:
                advice = "Стратегія збиткова за рахунок великих втрат. Слід збільшити співвідношення Take Profit / Stop Loss (tp2_rr) або знизити sl_atr_mult."
                
            return {
                "status": "active",
                "total_trades": total_trades,
                "win_rate": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2),
                "total_pnl": round(total_pnl, 2),
                "advice": advice
            }
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    def propose_optimization(self, df_dict: dict, base_config: dict) -> dict:
        """
        Запускає оптимізацію Grid Search на історичних даних для пошуку найкращих параметрів.
        """
        try:
            print(f"[{self.name}] Запуск Grid Search оптимізації для покращення параметрів...")
            opt_df = optimize(
                df_dict,
                grid=RACER_FAST_GRID,
                base_params=base_config,
                sort_by="net_pnl_pct",
                top_n=5,
                verbose=False,
                strategy_name="racer"
            )
            
            if opt_df.empty:
                return {}
            
            best_run = opt_df.iloc[0].to_dict()
            return {
                "fib_level": float(best_run.get("fib_level", base_config["fib_level"])),
                "sl_atr_mult": float(best_run.get("sl_atr_mult", base_config["sl_atr_mult"])),
                "tp1_rr": float(best_run.get("tp1_rr", base_config["tp1_rr"])),
                "tp2_rr": float(best_run.get("tp2_rr", base_config["tp2_rr"])),
                "net_pnl_pct": float(best_run.get("net_pnl_pct", 0.0)),
                "win_rate": float(best_run.get("win_rate", 0.0))
            }
        except Exception as e:
            print(f"[{self.name}] Помилка під час оптимізації: {e}")
            return {}


class DecisionAgent:
    """
    Агент прийняття рішень (Керуючий).
    Синтезує звіти від Pattern Analyzer та Trade Diagnostics, приймає рішення про оновлення active_config.json.
    """
    def __init__(self, config_path: str):
        self.name = "Decision Agent"
        self.config_path = config_path

    def load_active_config(self) -> dict:
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        return {}

    def save_active_config(self, config: dict):
        with open(self.config_path, 'w') as f:
            json.dump(config, f, indent=4)

    def evaluate_and_update(self, pattern_report: dict, diagnostics_report: dict, proposed_config: dict) -> bool:
        """Оцінює пропозицію оптимізації та оновлює конфігурацію бота при необхідності."""
        current_config = self.load_active_config()
        if not current_config:
            print(f"[{self.name}] Помилка: Не вдалося завантажити поточну конфігурацію.")
            return False
        
        # Перевіряємо чи є пропозиція від Оптимізатора і чи вона краща за поточний стан
        if not proposed_config:
            print(f"[{self.name}] Немає нових пропозицій для оптимізації. Залишаємо поточні налаштування.")
            self._send_status_update(pattern_report, diagnostics_report, current_config, updated=False)
            return False
        
        # Визначаємо чи потрібні зміни
        changes = []
        updated_config = current_config.copy()
        
        for key in ["fib_level", "sl_atr_mult", "tp1_rr", "tp2_rr"]:
            if key in proposed_config and proposed_config[key] != current_config.get(key):
                old_val = current_config.get(key)
                new_val = proposed_config[key]
                changes.append(f"• <code>{key}</code>: {old_val} ➡️ {new_val}")
                updated_config[key] = new_val
        
        if changes:
            self.save_active_config(updated_config)
            print(f"[{self.name}] ЗАТВЕРДЖЕНО оновлення параметрів: {', '.join(changes)}")
            self._send_status_update(pattern_report, diagnostics_report, updated_config, updated=True, changes=changes, proposed_info=proposed_config)
            return True
        else:
            print(f"[{self.name}] Пропозиція оптимізації збігається з поточними налаштуваннями.")
            self._send_status_update(pattern_report, diagnostics_report, current_config, updated=False)
            return False

    def _send_status_update(self, pattern_report: dict, diagnostics_report: dict, config: dict, updated: bool, changes: list = None, proposed_info: dict = None):
        """Формує та надсилає детальний звіт про ШІ-консенсус в Telegram."""
        
        # Секція Pattern Analyzer
        p_stats = pattern_report.get("success_rate_info", {"total_patterns": 0, "success_rate": 0.0})
        p_msg = (
            f"📈 <b>1. Pattern Analyzer Agent</b>\n"
            f"  • Свіжі паттерни (50 барів): {pattern_report.get('bullish_setups', 0)} LONG / {pattern_report.get('bearish_setups', 0)} SHORT\n"
            f"  • Історична успішність паттернів: <b>{p_stats.get('success_rate', 0.0)}%</b> (угод: {p_stats.get('total_patterns', 0)})\n"
        )
        if pattern_report.get("recent_setup"):
            rs = pattern_report["recent_setup"]
            p_msg += f"  • Останній знайдений сетап: <b>{rs['direction']}</b> на рівні {rs['entry']:.4f}\n"

        # Секція Trade Diagnostics
        d_status = diagnostics_report.get("status", "no_data")
        if d_status == "active":
            d_msg = (
                f"📊 <b>2. Trade Diagnostics Agent</b>\n"
                f"  • Загальний Win Rate: <b>{diagnostics_report.get('win_rate')}%</b>\n"
                f"  • Profit Factor: <b>{diagnostics_report.get('profit_factor')}</b>\n"
                f"  • Сумарний PnL: <b>{diagnostics_report.get('total_pnl'):+g} USDT</b>\n"
                f"  • Порада ШІ: <i>{diagnostics_report.get('advice')}</i>\n"
            )
        else:
            d_msg = (
                f"📊 <b>2. Trade Diagnostics Agent</b>\n"
                f"  • Статус: {d_status.upper()}\n"
                f"  • Порада: <i>Збираємо більше даних про торги на Демо для глибокого аналізу.</i>\n"
            )

        # Секція Decision Maker
        if updated:
            changes_str = "\n".join(changes)
            decision_msg = (
                f"⚖️ <b>3. Decision Agent (Керуючий)</b>\n"
                f"🟢 <b>ЗАТВЕРДЖЕНО оновлення параметрів!</b>\n"
                f"{changes_str}\n\n"
                f"🚀 Очікуваний результат з оптимізації:\n"
                f"  • Очікуваний Win Rate: <b>{proposed_info.get('win_rate', 0.0):.1f}%</b>\n"
                f"  • Прогнозований Net PnL: <b>{proposed_info.get('net_pnl_pct', 0.0):+.2f}%</b>"
            )
        else:
            decision_msg = (
                f"⚖️ <b>3. Decision Agent (Керуючий)</b>\n"
                f"🟡 <b>Параметри оптимальні.</b> Оновлення не потрібне.\n"
                f"Поточна конфігурація стратегії залишається активною."
            )

        full_message = (
            f"🧠 <b>Мульти-Агентний ШІ Консенсус</b> 🧠\n"
            f"<i>[Режим: ТРЕНУВАННЯ ТА ДЕМО-ТОРГІВЛЯ]</i>\n"
            f"----------------------------------------\n"
            f"{p_msg}\n"
            f"{d_msg}\n"
            f"{decision_msg}\n"
            f"----------------------------------------\n"
            f"⚙️ <b>Активні налаштування:</b>\n"
            f"  • Fib level: <b>{config.get('fib_level')}</b> | SL mult: <b>{config.get('sl_atr_mult')}</b>\n"
            f"  • TP1 RR: <b>{config.get('tp1_rr')}</b> | TP2 RR: <b>{config.get('tp2_rr')}</b>\n"
            f"  • ADX thresh: <b>{config.get('adx_thresh')}</b> | Vol mult: <b>{config.get('vol_mult')}</b>"
        )
        
        send_telegram_message(full_message)


def run_cooperative_agent_consensus(exchange, symbols: list, timeframe: str = "15m", config_path: str = "active_config.json"):
    """
    Головна точка входу для запуску консенсусу агентів.
    1. Pattern Analyzer сканує останні графіки.
    2. Trade Diagnostics перевіряє історію угод.
    3. При наявності даних запускається оптимізація.
    4. Decision Agent приймає рішення та оновлює конфіг.
    """
    print(f"\n🧠 [{datetime.now()}] Запуск Мульти-Агентного ШІ консенсусу...")
    
    analyzer = PatternAnalyzerAgent()
    diagnostician = TradeDiagnosticsAgent()
    decision_maker = DecisionAgent(config_path)
    
    current_config = decision_maker.load_active_config()
    if not current_config:
        print("Не вдалося завантажити конфігурацію.")
        return
    
    # 1. Збір ринкових даних для аналізу паттернів (беремо топ-монету, наприклад BTCUSDT)
    test_symbol = symbols[0] if symbols else "BTC/USDT:USDT"
    
    # Спробуємо безпечно завантажити дані для аналізу паттернів
    pattern_report = {}
    states_dict = {}
    try:
        from bybit_bot import fetch_data
        df = fetch_data(exchange, test_symbol, timeframe, limit=150)
        htf_df = fetch_data(exchange, test_symbol, "4h", limit=50)
        
        pattern_report = analyzer.analyze_market_patterns(df, htf_df, current_config)
        pattern_report["success_rate_info"] = analyzer.get_pattern_success_rate()
        
        states = analyze_racer(df, htf_df, current_config)
        states_dict[test_symbol] = states
    except Exception as e:
        print(f"Помилка при завантаженні ринкових даних для аналізу агентів: {e}")
        pattern_report = {
            "bullish_setups": 0, "bearish_setups": 0, "recent_setup": None,
            "success_rate_info": analyzer.get_pattern_success_rate()
        }

    # 2. Діагностика торгівлі
    diagnostics_report = diagnostician.run_diagnostics()
    
    # 3. Запуск оптимізації (якщо завантажились states та є хоча б якісь історичні дані або для профілактики раз на добу)
    proposed_config = {}
    if states_dict:
        proposed_config = diagnostician.propose_optimization(states_dict, current_config)

    # 4. Прийняття рішення
    decision_maker.evaluate_and_update(pattern_report, diagnostics_report, proposed_config)
    print(f"🧠 [{datetime.now()}] Консенсус агентів завершено!")

if __name__ == "__main__":
    # Тестовий запуск для діагностики
    from bybit_bot import init_bybit
    ex = init_bybit()
    run_cooperative_agent_consensus(ex, ["BTC/USDT:USDT"])
