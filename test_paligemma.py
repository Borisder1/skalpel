import os
import io
import base64
import requests
import json
import pandas as pd
import numpy as np
import mplfinance as mpf
from datetime import datetime, timedelta

PALIGEMMA_API_KEY = "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"

def generate_mock_chart() -> str:
    """Генерує свічковий графік і повертає його як Base64 рядок."""
    # Create mock OHLCV data for 50 periods
    dates = pd.date_range(end=datetime.now(), periods=50, freq='15min')
    np.random.seed(42)
    # Generate a random walk for prices
    returns = np.random.normal(0, 0.005, 50)
    prices = 60000 * np.exp(np.cumsum(returns))
    
    df = pd.DataFrame(index=dates)
    df['Open'] = prices * (1 + np.random.normal(0, 0.001, 50))
    df['Close'] = prices * (1 + np.random.normal(0, 0.001, 50))
    df['High'] = df[['Open', 'Close']].max(axis=1) * (1 + abs(np.random.normal(0, 0.002, 50)))
    df['Low'] = df[['Open', 'Close']].min(axis=1) * (1 - abs(np.random.normal(0, 0.002, 50)))
    df['Volume'] = np.random.randint(10, 1000, 50)
    
    # Draw chart in memory
    buf = io.BytesIO()
    # We use a clean style suitable for AI reading
    mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
    s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='', facecolor='white')
    
    # Save to buffer without axes to make it clean
    mpf.plot(df, type='candle', style=s, volume=True, savefig=dict(fname=buf, dpi=100, bbox_inches='tight', pad_inches=0.1))
    
    buf.seek(0)
    b64_image = base64.b64encode(buf.read()).decode('utf-8')
    return b64_image

def ask_paligemma(b64_image: str) -> str:
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
                    {"type": "text", "text": "What is the trend in this financial candlestick chart? Reply with exactly one word: BULLISH, BEARISH, or NEUTRAL."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
                ]
            }
        ],
        "max_tokens": 10,
        "temperature": 0.2,
        "top_p": 0.7,
        "stream": False
    }

    print(f"[{datetime.now()}] 👁️ Відправка графіка до PaliGemma...")
    try:
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e.response.text}")
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    print("Генерую графік...")
    b64 = generate_mock_chart()
    print("Графік згенеровано (довжина base64: {})".format(len(b64)))
    answer = ask_paligemma(b64)
    print("\n--- Відповідь PaliGemma ---")
    print(answer)
