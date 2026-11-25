---
name: smc-quant-auditor
description: Built-in trading, self-learning, and Render 512MB memory management skill for SMC Racer (Skalpel V13 IQTrend Pro Model).
---

# SMC Quant Auditor & Self-Evolution Skill (V13)

## 1. Core Trading Philosophy (IQTrend 4:1 Model)
- **Closed Candle Evaluation (`states[-2]`):** Always evaluate volume, ADX, FVG, and SMC structure on the last closed 15m candle (`states[-2]`), never on the 1-minute-old forming candle (`states[-1]`).
- **Live Market Anchor (`live_price`):** Because execution on Bybit uses Market orders, `entry`, `sl`, `tp1`, `tp2`, `tp3` must always be calculated from `live_price = float(df["close"].iloc[-1])`.
- **3-Stage Take Profit (`4 Wins : 1 Loss` Profile):**
  - `TP1 = 0.65R – 0.75R`: Closes 50% of position in profit and moves Stop-Loss to `+0.12R` (fee-plus breakeven).
  - `TP2 = 1.30R – 1.60R`: Main intraday target.
  - `TP3 = 2.10R – 2.40R`: 4H Supply/Demand macro target.
- **Hard Protection Filters:**
  - Never LONG when `RSI(14) > 67` or distance to 4H Resistance `< 0.65 ATR`.
  - Never SHORT when `RSI(14) < 33` or distance to 4H Support `< 0.65 ATR`.
  - Anti-climax: skip if 2-bar move `> 2.2 ATR`.
  - Anti-correlation: max 2 concurrent positions in the same direction (`LONG` or `SHORT`).

## 2. Render 512MB RAM Strict Rules
- Never load all 4,500 Bybit markets; keep only active USDT linear perpetuals in `exchange.markets` and strip raw `info` dicts.
- Fetch OHLCV via `async_scanner.py` direct REST (`https://api.bybit.com/v5/market/kline`) to avoid CCXT `exchange.ohlcvs` cache growth.
- Call `gc.collect()` and `ctypes.CDLL("libc.so.6").malloc_trim(0)` at the end of every scan cycle.

## 3. Render Shell Diagnostic One-Liner
```bash
echo "=== 1. RAM USAGE (LIMIT 512 MB) ===" && ps -o pid,rss,pmem,etime,cmd -C python | awk 'NR==1{print $0} NR>1{printf "%s %.1f MB (%s%%) uptime=%s %s %s %s\n", $1, $2/1024, $3, $4, $5, $6, $7}' && python export_bybit_trades.py && python audit_real_vs_virtual.py && echo "=== 4. ОСТАННІ ПОДІЇ В ЛОГАХ V13 ===" && tail -n 35 $(ls -t logs/bot_*.log | head -n 1)
```
