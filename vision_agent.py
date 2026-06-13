import os
import io
import base64
import requests
import pandas as pd
import mplfinance as mpf
from datetime import datetime

PALIGEMMA_API_KEY = os.getenv("PALIGEMMA_API_KEY", "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")

def ask_vision_oracle(df: pd.DataFrame, symbol: str) -> str:
    """
    Генерує графік з DataFrame та відправляє його в Nvidia Vision AI (Llama 3.2 Vision).
    Повертає "BULLISH", "BEARISH", або "NEUTRAL".
    """
    if df.empty or len(df) < 10:
        return "NEUTRAL"
        
    try:
        # Generate chart
        buf = io.BytesIO()
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='', facecolor='white')
        
        # We only plot the last 50 candles for a clean image
        plot_df = df.tail(50).copy()
        # mplfinance requires DateTimeIndex
        if not isinstance(plot_df.index, pd.DatetimeIndex):
            plot_df.index = pd.to_datetime(plot_df['timestamp'], unit='ms')
            
        # Ensure column names are standard
        req_cols = {'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}
        plot_df.rename(columns=req_cols, inplace=True)
            
        mpf.plot(plot_df, type='candle', style=s, volume=True, savefig=dict(fname=buf, dpi=80, bbox_inches='tight', pad_inches=0.1))
        
        buf.seek(0)
        b64_image = base64.b64encode(buf.read()).decode('utf-8')
        
        invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {PALIGEMMA_API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "meta/llama-3.2-11b-vision-instruct",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "You are an expert Quant Trader. Look at this candlestick chart. What is the immediate trend? Is there a liquidity trap? Reply with exactly one word: BULLISH, BEARISH, or NEUTRAL."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
                    ]
                }
            ],
            "max_tokens": 10,
            "temperature": 0.2,
            "stream": False
        }
        
        print(f"[{datetime.now()}] 👁️ Відправка графіка {symbol} до Vision AI...")
        for attempt in range(2):
            try:
                response = requests.post(invoke_url, headers=headers, json=payload, timeout=45)
                if response.status_code == 200:
                    content = response.json()["choices"][0]["message"]["content"].strip().upper()
                    if "BULL" in content: return "BULLISH"
                    if "BEAR" in content: return "BEARISH"
                    return "NEUTRAL"
                else:
                    print(f"[{datetime.now()}] ⚠️ Помилка Vision AI (Спроба {attempt+1}): {response.text}")
            except requests.exceptions.RequestException as e:
                print(f"[{datetime.now()}] ⚠️ Помилка запиту Vision AI (Спроба {attempt+1}): {e}")
                if attempt == 1:
                    return "NEUTRAL"
        return "NEUTRAL"
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Помилка генерації графіка: {e}")
        return "NEUTRAL"
