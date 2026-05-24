import json
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI(title="SMC Agent Diagnostics Bridge")

# Database Setup
def init_db():
    conn = sqlite3.connect('smc_diagnostics.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            type TEXT,
            ticker TEXT,
            tf TEXT,
            price REAL,
            trend TEXT,
            status TEXT,
            direction TEXT,
            grade TEXT,
            entry REAL,
            stop REAL,
            tp1 REAL,
            qty REAL,
            score REAL,
            pattern TEXT,
            probability REAL,
            block_reason TEXT,
            msg TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
        print(f"\n[🚀 {data['type']}] {data['ticker']} @ {data['price']}")
        
        # Log to Database
        conn = sqlite3.connect('smc_diagnostics.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO signals (
                timestamp, type, ticker, tf, price, trend, status, 
                direction, grade, entry, stop, tp1, qty, score, 
                pattern, probability, block_reason, msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(),
            data.get('type'),
            data.get('ticker'),
            data.get('tf'),
            data.get('price'),
            data.get('trend'),
            data.get('status'),
            data.get('plan', {}).get('dir'),
            data.get('plan', {}).get('grade'),
            data.get('plan', {}).get('entry'),
            data.get('plan', {}).get('stop'),
            data.get('plan', {}).get('tp1'),
            data.get('plan', {}).get('qty'),
            data.get('plan', {}).get('score'),
            data.get('diag', {}).get('pattern'),
            data.get('diag', {}).get('prob'),
            data.get('diag', {}).get('blockReason'),
            data.get('msg')
        ))
        conn.commit()
        conn.close()
        
        # Simple Analysis Logic (Future evolution)
        if data['type'] == 'EXIT':
            print(f"   📊 Trade Finished: {data['msg']}")
        
        return {"status": "success"}
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    print("🛰️ SMC Neural Bridge is starting...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
