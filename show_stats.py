import sqlite3
import json
import os

def main():
    # V11: Persistent Disk
    _data_dir = "/data" if os.path.isdir("/data") else "."
    db_path = os.path.join(_data_dir, "trades_history.db")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
    except Exception as e:
        print(f"Помилка підключення до бази даних: {e}")
        return

    print("=" * 60)
    print("📊 ПОВНИЙ ЗВІТ SMC RACER V11")
    print("=" * 60)

    # 1. Загальна статистика по статусах
    cursor.execute("SELECT status, COUNT(*), ROUND(SUM(pnl), 2) FROM trades GROUP BY status;")
    status_data = cursor.fetchall()
    print("\n[СТАТУСИ УГОД]")
    for row in status_data:
        print(f"  {row[0]:<15} | Кількість: {row[1]:<5} | PnL: {row[2] or 0.0}")

    # 2. Win Rate
    cursor.execute('''
        SELECT 
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
            ROUND(SUM(pnl), 2) as total_pnl
        FROM trades 
        WHERE status IN ('WIN', 'LOSS', 'VIRTUAL_WIN', 'VIRTUAL_LOSS')
    ''')
    wr_data = cursor.fetchone()
    wins = wr_data[0] or 0 if wr_data else 0
    losses = wr_data[1] or 0 if wr_data else 0
    total_pnl = wr_data[2] or 0.0 if wr_data else 0.0
    total_closed = wins + losses
    if total_closed > 0:
        win_rate = (wins / total_closed) * 100
        pf_q = cursor.execute('''
            SELECT ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END) / 
                   MAX(ABS(SUM(CASE WHEN pnl<0 THEN pnl ELSE 0 END)), 0.01), 2)
            FROM trades WHERE status IN ('VIRTUAL_WIN','VIRTUAL_LOSS','WIN','LOSS')
        ''').fetchone()
        pf = pf_q[0] or 0.0 if pf_q else 0.0
        print(f"\n[WIN RATE] {win_rate:.1f}% ({wins}W / {losses}L) | PnL: {total_pnl} USDT | PF: {pf}")
    else:
        print("\n[WIN RATE] Немає закритих угод для розрахунку.")

    # 3. Score Distribution
    cursor.execute('''
        SELECT 
            CASE 
                WHEN quant_score >= 0.85 THEN '0.85+' 
                WHEN quant_score >= 0.75 THEN '0.75-0.84' 
                WHEN quant_score >= 0.65 THEN '0.65-0.74' 
                ELSE '<0.65' 
            END as bracket,
            COUNT(*),
            ROUND(SUM(pnl), 2),
            ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / MAX(COUNT(*), 1), 1)
        FROM trades 
        WHERE status IN ('VIRTUAL_WIN', 'VIRTUAL_LOSS', 'WIN', 'LOSS')
        GROUP BY bracket 
        ORDER BY bracket DESC
    ''')
    score_data = cursor.fetchall()
    if score_data:
        print("\n[АНАЛІЗ SCORE]")
        for row in score_data:
            print(f"  Score {row[0]:<9} | Угод: {row[1]:<5} | PnL: {row[2] or 0.0} | WR: {row[3] or 0.0}%")
    else:
        print("\n[АНАЛІЗ SCORE] Немає закритих угод.")

    # 3b. ALL scores distribution (including open)
    cursor.execute('''
        SELECT 
            CASE 
                WHEN quant_score >= 0.90 THEN '0.90+' 
                WHEN quant_score >= 0.85 THEN '0.85-0.89' 
                WHEN quant_score >= 0.80 THEN '0.80-0.84' 
                WHEN quant_score >= 0.75 THEN '0.75-0.79' 
                WHEN quant_score >= 0.70 THEN '0.70-0.74' 
                ELSE '<0.70' 
            END as bracket,
            COUNT(*)
        FROM trades 
        GROUP BY bracket 
        ORDER BY bracket DESC
    ''')
    all_scores = cursor.fetchall()
    if all_scores:
        print("\n[РОЗПОДІЛ СКОРІВ (ВСІ УГОДИ)]")
        for row in all_scores:
            bar = "█" * min(row[1], 40)
            print(f"  {row[0]:<10} | {row[1]:<4} {bar}")

    # 4. Відкриті позиції
    cursor.execute("SELECT id, symbol, direction, ROUND(quant_score,2), status FROM trades WHERE status LIKE '%OPEN%' ORDER BY id DESC;")
    open_data = cursor.fetchall()
    print(f"\n[ВІДКРИТІ ПОЗИЦІЇ: {len(open_data)}]")
    for row in open_data[:15]:
        print(f"  #{row[0]:<4} | {row[1]:<22} | {row[2]:<5} | Score: {row[3]} | {row[4]}")
    if len(open_data) > 15:
        print(f"  ... та ще {len(open_data) - 15}")

    # 5. Blacklist
    cursor.execute("SELECT symbol, loss_count, expires_at, reason FROM symbol_blacklist WHERE expires_at > datetime('now') ORDER BY loss_count DESC LIMIT 10;")
    bl_data = cursor.fetchall()
    print(f"\n[АКТИВНИЙ BLACKLIST: {len(bl_data)}]")
    if not bl_data:
        print("  Порожньо")
    for row in bl_data:
        print(f"  {row[0]:<22} | Strikes: {row[1]} | До: {row[2]} | {row[3] or ''}")

    # 6. Cancelled analysis
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'CANCELLED'")
    cancelled = cursor.fetchone()[0] or 0
    if cancelled > 0:
        print(f"\n[СКАСОВАНІ ОРДЕРИ: {cancelled}]")
        cursor.execute('''
            SELECT symbol, COUNT(*) as cnt FROM trades 
            WHERE status='CANCELLED' GROUP BY symbol ORDER BY cnt DESC LIMIT 5
        ''')
        for row in cursor.fetchall():
            print(f"  {row[0]:<22} | Скасувань: {row[1]}")

    # 7. Daily breakdown
    cursor.execute('''
        SELECT date(timestamp) as day, 
               COUNT(*) as trades,
               SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as losses,
               ROUND(SUM(pnl),2) as day_pnl
        FROM trades 
        WHERE status IN ('VIRTUAL_WIN','VIRTUAL_LOSS','WIN','LOSS') 
          AND timestamp >= date('now', '-7 days')
        GROUP BY day ORDER BY day
    ''')
    daily = cursor.fetchall()
    if daily:
        print("\n[ЩОДЕННА СТАТИСТИКА]")
        for row in daily:
            wr = (row[2] or 0) / max(row[1], 1) * 100
            print(f"  {row[0]} | Угод: {row[1]:<4} | W: {row[2] or 0} L: {row[3] or 0} | PnL: {row[4] or 0.0} | WR: {wr:.0f}%")

    # 8. Feature Manager Status
    features_file = os.path.join(_data_dir, "features.json")
    if os.path.exists(features_file):
        try:
            with open(features_file, "r") as f:
                features = json.load(f)
            print("\n[ADAPTIVE FEATURES]")
            for name, data in features.items():
                status = "🟢 ON" if data.get("enabled") else ("👁 SHADOW" if data.get("shadow") else "🔴 OFF")
                total = data.get("win", 0) + data.get("loss", 0)
                wr = f"{data['win']/total*100:.0f}%" if total > 0 else "N/A"
                print(f"  {name:<25} | {status:<10} | W:{data.get('win',0)} L:{data.get('loss',0)} WR:{wr}")
        except Exception:
            pass

    print("\n" + "=" * 60)
    conn.close()

if __name__ == "__main__":
    main()
