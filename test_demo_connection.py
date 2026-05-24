import json
import os
from datetime import datetime, timezone
import ccxt

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_config.json")


def print_result(ok: bool, step: str, reason: str = ""):
    icon = "✅" if ok else "❌"
    print(f"{icon} {step}" + (f" — {reason}" if reason else ""))


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg = load_config()
    use_demo = bool(cfg.get("use_demo", True))
    base_url = cfg.get("base_url") or "https://api-demo.bybit.com"
    api_key = cfg.get("api_key") or os.getenv("BYBIT_API_KEY", "")
    api_secret = cfg.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")

    if not use_demo:
        print_result(False, "Config safety check", "use_demo=false; test aborted to avoid live mode")
        return

    exchange = ccxt.bybit({
        "enableRateLimit": True,
        "apiKey": api_key,
        "secret": api_secret,
        "urls": {"api": base_url},
        "options": {"defaultType": "future", "recvWindow": 10000},
    })

    print(f"[{datetime.now(timezone.utc).isoformat()}] Using base URL: {base_url}")

    try:
        exchange.enableDemoTrading(True)
    except Exception:
        pass

    symbol = "BTC/USDT:USDT"
    order_id = None

    # 1) connect + server time
    try:
        t = exchange.fetch_time()
        print_result(True, "Connect to demo API", f"server_time={t}")
    except Exception as e:
        print_result(False, "Connect to demo API", str(e))
        return

    # 2) balance
    try:
        bal = exchange.fetch_balance()
        usdt = (bal.get("USDT") or {}).get("free", "n/a")
        print_result(True, "Fetch demo balance", f"USDT free={usdt}")
    except Exception as e:
        print_result(False, "Fetch demo balance", str(e))

    # 3) candles
    try:
        kl = exchange.fetch_ohlcv(symbol, "15m", limit=10)
        print_result(True, "Fetch BTCUSDT 10 candles", f"bars={len(kl)}")
    except Exception as e:
        print_result(False, "Fetch BTCUSDT 10 candles", str(e))

    # 4) place tiny test order
    try:
        ticker = exchange.fetch_ticker(symbol)
        px = float(ticker["last"])
        markets = exchange.load_markets()
        m = markets[symbol]
        min_amt = float((m.get("limits", {}).get("amount", {}).get("min") or 0.001))
        price = exchange.price_to_precision(symbol, px * 0.90)
        amount = exchange.amount_to_precision(symbol, min_amt)
        order = exchange.create_order(symbol, "limit", "buy", float(amount), float(price), {"timeInForce": "PostOnly"})
        order_id = order.get("id")
        print_result(True, "Place test min-size order", f"id={order_id} amount={amount} price={price}")
    except Exception as e:
        print_result(False, "Place test min-size order", str(e))

    # 5) status
    if order_id:
        try:
            st = exchange.fetch_order(order_id, symbol)
            print_result(True, "Check order status", f"status={st.get('status')}")
        except Exception as e:
            print_result(False, "Check order status", str(e))

    # 6) cancel
    if order_id:
        try:
            exchange.cancel_order(order_id, symbol)
            print_result(True, "Cancel test order", f"id={order_id}")
        except Exception as e:
            print_result(False, "Cancel test order", str(e))


if __name__ == "__main__":
    main()
