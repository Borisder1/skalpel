"""
V11: One-time fix for existing VIRTUAL_OPEN trades with bytes values.
Run on Render: python fix_bytes_trades.py
"""
import sqlite3
import struct

def fix():
    conn = sqlite3.connect("trades_history.db")
    cursor = conn.cursor()
    
    # Find all trades with bytes values
    cursor.execute("SELECT id, entry_price, stop_loss, take_profit_1, take_profit_2 FROM trades WHERE status IN ('VIRTUAL_OPEN', 'OPEN')")
    rows = cursor.fetchall()
    
    fixed = 0
    for row in rows:
        tid = row[0]
        needs_fix = False
        vals = list(row[1:])
        
        for i, val in enumerate(vals):
            if isinstance(val, bytes):
                needs_fix = True
                try:
                    vals[i] = struct.unpack('f', val)[0]
                except:
                    vals[i] = 0.0
            elif val is not None:
                vals[i] = float(val)
        
        if needs_fix:
            cursor.execute(
                "UPDATE trades SET entry_price=?, stop_loss=?, take_profit_1=?, take_profit_2=? WHERE id=?",
                (*vals, tid)
            )
            fixed += 1
            print(f"Fixed trade #{tid}: entry={vals[0]}, sl={vals[1]}, tp1={vals[2]}, tp2={vals[3]}")
    
    conn.commit()
    conn.close()
    print(f"\n✅ Fixed {fixed} trades with bytes values. Total checked: {len(rows)}")

if __name__ == "__main__":
    fix()
