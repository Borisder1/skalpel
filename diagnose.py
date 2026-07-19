import os
import sys
import time
import sqlite3
import json
import subprocess
from datetime import datetime

print("=" * 60)
print("🔍 SYSTEM DIAGNOSTIC TOOL FOR SMC RACER")
print("=" * 60)

# 1. Check Python and environment
print(f"\n[1. ENVIRONMENT]")
print(f"Python Version: {sys.version}")
print(f"Current Directory: {os.getcwd()}")
print(f"Environment variables:")
print(f"  - PORT: {os.getenv('PORT', 'Not set')}")
print(f"  - BYBIT_API_KEY: {'Present' if os.getenv('BYBIT_API_KEY') else 'MISSING ❌'}")
print(f"  - BYBIT_API_SECRET: {'Present' if os.getenv('BYBIT_API_SECRET') else 'MISSING ❌'}")
print(f"  - TELEGRAM_BOT_TOKEN: {'Present' if os.getenv('TELEGRAM_BOT_TOKEN') else 'MISSING ❌'}")

# 2. Check running processes
print(f"\n[2. RUNNING PROCESSES]")
try:
    proc = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
    python_procs = [line for line in proc.stdout.split('\n') if 'python' in line or 'bybit_bot' in line]
    if python_procs:
        print("Running bot processes found:")
        for p in python_procs:
            print(f"  {p}")
    else:
        print("❌ No running python/bybit_bot processes found in ps aux!")
except Exception as e:
    print(f"Could not run ps aux: {e}")

# 3. Check configuration files
print(f"\n[3. CONFIGURATION]")
_data_dir = "/data" if os.path.isdir("/data") else "."
config_path = os.path.join(_data_dir, "active_config.json")
print(f"Expected Config Path: {config_path}")
if os.path.exists(config_path):
    print(f"Config exists (Size: {os.path.getsize(config_path)} bytes)")
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
        print("Config parameters:")
        print(f"  - use_demo: {cfg.get('use_demo')}")
        print(f"  - risk_pct: {cfg.get('risk_pct')}")
        print(f"  - max_concurrent_positions: {cfg.get('max_concurrent_positions')}")
        print(f"  - max_position_loss_usd: {cfg.get('max_position_loss_usd')}")
    except Exception as e:
        print(f"❌ Failed to parse config JSON: {e}")
else:
    print("❌ Config file does not exist!")

# 4. Check Database
print(f"\n[4. DATABASE]")
db_path = os.path.join(_data_dir, "trades_history.db")
print(f"Expected DB Path: {db_path}")
if os.path.exists(db_path):
    mtime = os.path.getmtime(db_path)
    print(f"DB File Size: {os.path.getsize(db_path)} bytes")
    print(f"DB Last Modified: {datetime.fromtimestamp(mtime)}")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) FROM trades GROUP BY status;")
        rows = cursor.fetchall()
        print("Trade statuses:")
        for r in rows:
            print(f"  - {r[0]}: {r[1]}")
        
        cursor.execute("SELECT id, timestamp, symbol, direction, status, pnl FROM trades ORDER BY id DESC LIMIT 5;")
        last_trades = cursor.fetchall()
        print("Last 5 trades:")
        for t in last_trades:
            print(f"  ID: {t[0]} | {t[1]} | {t[2]} | {t[3]} | {t[4]} | PnL: {t[5]}")
        conn.close()
    except Exception as e:
        print(f"❌ DB Query failed: {e}")
else:
    print("❌ Database file does not exist!")

# 5. Check Logs
print(f"\n[5. LOG FILES]")
log_dirs = ["logs", _data_dir]
log_files = []
for d in log_dirs:
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.endswith('.log'):
                log_files.append(os.path.join(d, f))

if log_files:
    # Sort by modification time
    log_files.sort(key=os.path.getmtime, reverse=True)
    print(f"Found {len(log_files)} log files. Most recent: {log_files[0]}")
    mtime = os.path.getmtime(log_files[0])
    print(f"Last modified: {datetime.fromtimestamp(mtime)}")
    print("Last 20 lines of recent log:")
    try:
        with open(log_files[0], 'r', encoding='utf-8', errors='ignore') as lf:
            lines = lf.readlines()
        for line in lines[-20:]:
            print(f"  {line.strip()}")
    except Exception as e:
        print(f"Could not read log file: {e}")
else:
    print("❌ No log files (.log) found in project directory or /data!")

# 6. Test Bybit CCXT Connection
print(f"\n[6. BYBIT CCXT API TEST]")
try:
    import ccxt
    from dotenv import load_dotenv
    load_dotenv()
    
    use_demo = True
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                use_demo = json.load(f).get("use_demo", True)
        except:
            pass
            
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    
    exchange_params = {
        'enableRateLimit': True,
        'timeout': 15000,
        'options': {'defaultType': 'future'}
    }
    if use_demo:
        exchange_params['urls'] = {'api': "https://api-demo.bybit.com"}
    else:
        exchange_params['urls'] = {'api': "https://api.bybit.com"}
        
    if api_key and api_secret:
        exchange_params['apiKey'] = api_key
        exchange_params['secret'] = api_secret
        print("Attempting authenticated API call...")
    else:
        print("API keys missing from environment. Skipping authenticated API call...")
        
    ex = ccxt.bybit(exchange_params)
    if use_demo:
        try:
            ex.enableDemoTrading(True)
        except:
            pass
            
    print("Fetching tickers for BTC/USDT...")
    ticker = ex.fetch_ticker("BTC/USDT")
    print(f"  BTC/USDT Last Price: {ticker.get('last')}")
    
    if api_key and api_secret:
        print("Fetching balance...")
        bal = ex.fetch_balance()
        usdt_bal = bal.get("USDT", {})
        print(f"  USDT Wallet Balance: {usdt_bal.get('total') or usdt_bal.get('walletBalance') or 'N/A'}")
except Exception as e:
    print(f"❌ API Test failed: {e}")

print("\n" + "=" * 60)
print("DIAGNOSTICS COMPLETED")
print("=" * 60)
