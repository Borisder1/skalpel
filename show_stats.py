import sqlite3
import datetime

def main():
    db_path = "trades_history.db"
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
    except Exception as e:
        print(f"Помилка підключення до бази даних: {e}")
        return

    print("="*50)
    print("📊 ПОВНИЙ ЗВІТ SMC RACER V10")
    print("="*50)

    # 1. Загальна статистика
    cursor.execute("SELECT status, COUNT(*), ROUND(SUM(pnl), 2) FROM trades GROUP BY status;")
    status_data = cursor.fetchall()
    print("\n[СТАТУСИ УГОД]")
    for row in status_data:
        print(f"{row[0]:<15} | Кількість: {row[1]:<5} | PnL: {row[2]}")

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
    if wr_data and (wr_data[0] + wr_data[1]) > 0:
        wins, losses, total = wr_data[0], wr_data[1], wr_data[2]
        total_trades = wins + losses
        win_rate = (wins / total_trades) * 100
        print(f"\n[WIN RATE] {win_rate:.1f}% ({wins} Win / {losses} Loss) | Total PnL: {total}")
    else:
        print("\n[WIN RATE] Немає закритих угод для розрахунку.")

    # 3. Аналіз Score (Чи працює Threshold)
    cursor.execute('''
        SELECT 
            CASE 
                WHEN quant_score >= 0.85 THEN '0.85+' 
                WHEN quant_score >= 0.70 THEN '0.70-0.84' 
                WHEN quant_score >= 0.60 THEN '0.60-0.69' 
                ELSE '<0.60' 
            END as score_bracket,
            COUNT(*),
            ROUND(SUM(pnl), 2)
        FROM trades 
        WHERE status IN ('VIRTUAL_WIN', 'VIRTUAL_LOSS', 'WIN', 'LOSS')
        GROUP BY score_bracket 
        ORDER BY score_bracket DESC
    ''')
    score_data = cursor.fetchall()
    print("\n[АНАЛІЗ SCORE]")
    for row in score_data:
        print(f"Score {row[0]:<9} | Угод: {row[1]:<5} | PnL: {row[2]}")

    # 4. Відкриті позиції
    cursor.execute("SELECT id, symbol, direction, quant_score, status FROM trades WHERE status LIKE '%OPEN%' ORDER BY id DESC;")
    open_data = cursor.fetchall()
    print(f"\n[ВІДКРИТІ ПОЗИЦІЇ: {len(open_data)}]")
    for i, row in enumerate(open_data[:10]):
        print(f"#{row[0]:<4} | {row[1]:<12} | {row[2]:<5} | Score: {row[3]} | {row[4]}")
    if len(open_data) > 10:
        print(f"... та ще {len(open_data) - 10} позицій.")

    # 5. Blacklist
    cursor.execute("SELECT symbol, loss_count, expires_at FROM symbol_blacklist WHERE expires_at > datetime('now') ORDER BY loss_count DESC LIMIT 5;")
    bl_data = cursor.fetchall()
    print(f"\n[АКТИВНИЙ BLACKLIST]")
    if not bl_data:
        print("Порожньо")
    for row in bl_data:
        print(f"{row[0]:<12} | Strikes: {row[1]} | До: {row[2]}")

    print("\n" + "="*50)
    conn.close()

if __name__ == "__main__":
    main()
