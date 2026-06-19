import os
import json
import random
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history.db")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_config.json")

def fitness(genome: dict) -> float:
    """
    Evaluates the genome by doing a simple backtest over the trades in DB.
    Genome specifies thresholds: {adx_min, vol_min, fvg_min}
    We assume if a trade's factors were below threshold, it wouldn't have been taken.
    Returns simulated PnL.
    """
    if not os.path.exists(DB_PATH):
        return 0.0
        
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT pnl, factors_snapshot FROM trades WHERE status IN ('CLOSED', 'VIRTUAL_CLOSED')")
    trades = cursor.fetchall()
    conn.close()
    
    if not trades:
        return 0.0
        
    sim_pnl = 0.0
    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    peak_pnl = 0.0
    max_dd = 0.0

    for row in trades:
        try:
            factors = json.loads(row["factors_snapshot"])
            if not factors: continue
            
            pnl = float(row["pnl"])
            # Genome is a set of weights.
            total_score = sum(factors.get(k, 0) * v for k, v in genome.items())
            
            # V10: Threshold update
            if total_score >= 0.80: # execution threshold
                sim_pnl += pnl
                if pnl > 0:
                    wins += 1
                    gross_profit += pnl
                else:
                    losses += 1
                    gross_loss += abs(pnl)
                
                if sim_pnl > peak_pnl:
                    peak_pnl = sim_pnl
                dd = peak_pnl - sim_pnl
                if dd > max_dd:
                    max_dd = dd

        except:
            continue
            
    total_trades = wins + losses
    if total_trades == 0:
        return 0.0
        
    win_rate = wins / total_trades
    profit_factor = gross_profit / max(gross_loss, 1e-5)
    
    # V10: Покращена фітнес-функція (Profit Factor * Win Rate Penalty * Drawdown Penalty)
    # Якщо WR < 40% — сильний штраф
    wr_modifier = win_rate ** 0.5
    
    # Drawdown penalty: якщо max_dd > 500 (наприклад), штрафуємо
    dd_penalty = max(1.0 - (max_dd / 1000.0), 0.1)
    
    fitness_score = profit_factor * wr_modifier * dd_penalty * sim_pnl
    
    # Якщо збиткова стратегія, повертаємо 0
    if sim_pnl < 0:
        return 0.0
        
    return fitness_score

def create_initial_population(base_weights: dict, size: int = 10) -> list:
    pop = []
    for _ in range(size):
        genome = {}
        for k, v in base_weights.items():
            genome[k] = max(0.01, v + random.uniform(-0.1, 0.1))
        # Normalize
        total = sum(genome.values())
        genome = {k: val / total for k, val in genome.items()}
        pop.append(genome)
    return pop

def crossover(g1: dict, g2: dict) -> dict:
    child = {}
    for k in g1.keys():
        child[k] = g1[k] if random.random() > 0.5 else g2[k]
    # Normalize
    total = sum(child.values())
    return {k: v / total for k, v in child.items()}

def mutate(genome: dict, mutation_rate=0.1) -> dict:
    mutated = {}
    for k, v in genome.items():
        if random.random() < mutation_rate:
            mutated[k] = max(0.01, v + random.uniform(-0.05, 0.05))
        else:
            mutated[k] = v
    total = sum(mutated.values())
    return {k: val / total for k, val in mutated.items()}

def run_evolution(generations=5):
    from quant_engine import _load_weights, _save_weights
    base = _load_weights()
    population = create_initial_population(base, size=20)
    
    print(f"[{datetime.now()}] 🧬 Запуск Генетичного Алгоритму (Поколінь: {generations})...")
    
    for gen in range(generations):
        # Evaluate fitness
        scored = [(genome, fitness(genome)) for genome in population]
        scored.sort(key=lambda x: x[1], reverse=True)
        
        best_pnl = scored[0][1]
        print(f"Покоління {gen+1}: Найкращий PnL = {best_pnl:.2f}")
        
        # Select top 4
        survivors = [s[0] for s in scored[:4]]
        
        # Breed new generation
        next_gen = survivors[:]
        while len(next_gen) < 20:
            p1 = random.choice(survivors)
            p2 = random.choice(survivors)
            child = crossover(p1, p2)
            child = mutate(child)
            next_gen.append(child)
            
        population = next_gen
        
        population = next_gen
        
    best_genome = population[0]
    
    # Generate Report
    report = f"🧬 Еволюцію завершено. Симульований PnL: {best_pnl:.2f} USDT.\n"
    increased = []
    decreased = []
    for k in base.keys():
        old_w = base.get(k, 0)
        new_w = best_genome.get(k, 0)
        diff = new_w - old_w
        if diff > 0.02:
            increased.append(f"{k} (+{diff:.1%})")
        elif diff < -0.02:
            decreased.append(f"{k} ({diff:.1%})")
            
    if increased: report += f"📈 Посилили фактори: {', '.join(increased)}\n"
    if decreased: report += f"📉 Зменшили фактори: {', '.join(decreased)}\n"
    if not increased and not decreased: report += "⚖️ Ваги майже не змінилися.\n"
    
    import db_logger
    db_logger.save_ai_memory("GENETIC_EVOLUTION", best_genome, "UNKNOWN", best_pnl, report)
    
    _save_weights(best_genome, {
        "total_learned": 0,
        "last_learn": datetime.now().isoformat(),
        "type": "GENETIC_EVOLUTION",
        "simulated_pnl": best_pnl
    })
    print(f"[{datetime.now()}] ✅ Еволюцію завершено. Новий геном збережено в AI Memory.")
    return report

if __name__ == "__main__":
    run_evolution()
