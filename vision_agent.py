import os
import io
import base64
import requests
import json
import pandas as pd
import mplfinance as mpf
from datetime import datetime

# Ключі безкоштовних Vision AI провайдерів
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
PALIGEMMA_API_KEY = os.getenv("PALIGEMMA_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "")

def _ask_gemini_vision(b64_image: str) -> str:
    """Виклик безкоштовного Google Gemini 1.5/2.0 Flash через Google AI Studio."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [
                {"text": "You are an expert Quant Trader. Look at this candlestick chart. What is the immediate trend and structure? Reply with exactly one word: BULLISH, BEARISH, or NEUTRAL."},
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": b64_image
                    }
                }
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 10
        }
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    if resp.status_code == 200:
        data = resp.json()
        text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip().upper()
        if "BULL" in text: return "BULLISH"
        if "BEAR" in text: return "BEARISH"
        return "NEUTRAL"
    else:
        print(f"[{datetime.now()}] ⚠️ Gemini Vision error: {resp.status_code} {resp.text[:100]}")
        return "NEUTRAL"

def _ask_groq_vision(b64_image: str) -> str:
    """Виклик безкоштовного Groq Vision (Llama 3.2 11B Vision)."""
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": VISION_MODEL or "llama-3.2-11b-vision-preview",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "You are an expert Quant Trader. Look at this candlestick chart. Reply with exactly one word: BULLISH, BEARISH, or NEUTRAL."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
            ]
        }],
        "max_tokens": 10,
        "temperature": 0.1
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    if resp.status_code == 200:
        text = resp.json()["choices"][0]["message"]["content"].strip().upper()
        if "BULL" in text: return "BULLISH"
        if "BEAR" in text: return "BEARISH"
        return "NEUTRAL"
    else:
        print(f"[{datetime.now()}] ⚠️ Groq Vision error: {resp.status_code} {resp.text[:100]}")
        return "NEUTRAL"

def _ask_openrouter_vision(b64_image: str) -> str:
    """Виклик OpenRouter безкоштовних моделей (:free)."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": VISION_MODEL or "google/gemini-2.0-flash-exp:free",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "You are an expert Quant Trader. Look at this candlestick chart. Reply with exactly one word: BULLISH, BEARISH, or NEUTRAL."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}}
            ]
        }],
        "max_tokens": 10,
        "temperature": 0.1
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    if resp.status_code == 200:
        text = resp.json()["choices"][0]["message"]["content"].strip().upper()
        if "BULL" in text: return "BULLISH"
        if "BEAR" in text: return "BEARISH"
        return "NEUTRAL"
    return "NEUTRAL"

def ask_vision_oracle(df: pd.DataFrame, symbol: str) -> str:
    """
    Генерує свічковий графік та відправляє у підключену Vision AI модель.
    Пріоритет безкоштовних моделей: Gemini Flash > Groq > OpenRouter.
    Якщо ключів немає — повертає "NEUTRAL" без зависань і зайвих помилок.
    """
    if df.empty or len(df) < 10:
        return "NEUTRAL"

    # Якщо жоден AI Vision ключ не вказано — не генеруємо картинку даремно (економія CPU)
    if not (GEMINI_API_KEY or GROQ_API_KEY or OPENROUTER_API_KEY or PALIGEMMA_API_KEY):
        return "NEUTRAL"

    try:
        buf = io.BytesIO()
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='', facecolor='white')
        
        plot_df = df.tail(50).copy()
        if not isinstance(plot_df.index, pd.DatetimeIndex):
            plot_df.index = pd.to_datetime(plot_df['timestamp'], unit='ms')
            
        req_cols = {'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}
        plot_df.rename(columns=req_cols, inplace=True)
            
        mpf.plot(plot_df, type='candle', style=s, volume=True, savefig=dict(fname=buf, dpi=80, bbox_inches='tight', pad_inches=0.1))
        buf.seek(0)
        b64_image = base64.b64encode(buf.read()).decode('utf-8')

        print(f"[{datetime.now()}] 👁️ Аналіз графіка {symbol} через Vision AI...")

        # 1. Google Gemini Flash (найпотужніший безкоштовний Vision)
        if GEMINI_API_KEY:
            try:
                res = _ask_gemini_vision(b64_image)
                if res != "NEUTRAL":
                    return res
            except Exception as e:
                print(f"[{datetime.now()}] ⚠️ Gemini Vision exc: {e}")

        # 2. Groq Cloud Vision (Llama 3.2 Vision)
        if GROQ_API_KEY:
            try:
                res = _ask_groq_vision(b64_image)
                if res != "NEUTRAL":
                    return res
            except Exception as e:
                print(f"[{datetime.now()}] ⚠️ Groq Vision exc: {e}")

        # 3. OpenRouter Free Models
        if OPENROUTER_API_KEY:
            try:
                res = _ask_openrouter_vision(b64_image)
                if res != "NEUTRAL":
                    return res
            except Exception as e:
                print(f"[{datetime.now()}] ⚠️ OpenRouter Vision exc: {e}")

        return "NEUTRAL"
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Помилка генерації графіка: {e}")
        return "NEUTRAL"
