import os
import sys
import json

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

def update_keys():
    """Оновлює Bybit API ключі. Використовує аргументи або env vars (НЕ захардкожені)."""
    if len(sys.argv) >= 3:
        api_key = sys.argv[1].strip()
        api_secret = sys.argv[2].strip()
    else:
        # Беремо з env vars (Render Dashboard)
        api_key = os.getenv("BYBIT_API_KEY", "").strip()
        api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
        if not api_key or not api_secret:
            print("❌ Використання: python update_keys.py <API_KEY> <API_SECRET>")
            print("   Або встанови BYBIT_API_KEY та BYBIT_API_SECRET в env vars.")
            return

    data_dir = "/data" if os.path.isdir("/data") else "."
    keys_file = os.path.join(data_dir, "bybit_keys.json")

    with open(keys_file, "w", encoding="utf-8") as f:
        json.dump({
            "api_key": api_key,
            "api_secret": api_secret
        }, f, indent=2)

    print(f"✅ Успішно збережено нові Bybit ключі у {keys_file}!")
    print(f"🔑 API Key: ...{api_key[-4:]}")

    # Перевіримо підключення
    print("\n🔍 Перевірка підключення до Bybit Demo...")
    try:
        import ccxt
        ex = ccxt.bybit({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'urls': {'api': 'https://api-demo.bybit.com'},
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
                'recvWindow': 20000
            }
        })
        ex.enableDemoTrading(True)
        try:
            ex.load_time_difference()
        except:
            pass
        bal = ex.fetch_balance()
        usdt = bal.get('USDT', {})
        total = usdt.get('total') or usdt.get('walletBalance') or 'N/A'
        print(f"🎉 Підключення успішне! Demo Баланс USDT: {total}")
    except Exception as e:
        print(f"⚠️ Помилка перевірки балансу: {e}")

if __name__ == "__main__":
    update_keys()
