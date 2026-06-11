import os
import requests
import json
from datetime import datetime

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "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")

def ask_kimi_reflection(trades_context: str) -> dict:
    """
    Відправляє звіт про збитки до Nvidia Kimi 2.6.
    Повертає словник з оцінкою поточного режиму ринку та рекомендацією.
    """
    invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    system_prompt = (
        "You are an elite Institutional Quant Trader. "
        "Your bot just hit 3 consecutive stop losses. Analyze the provided trade logs and market context. "
        "Determine the current 'Market Regime' (TREND, CHOP, VOLATILE, or MANIPULATION) "
        "and provide a short recommendation. Output your response STRICTLY as a JSON object: "
        "{\"regime\": \"CHOP\", \"reasoning\": \"Your explanation...\", \"recommendation\": \"Pause trading or widen stops\"}"
    )

    payload = {
        "model": "moonshotai/kimi-k2.6",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Recent Trades and Context:\n{trades_context}"}
        ],
        "max_tokens": 1024,
        "temperature": 0.3, # lower temperature for JSON
        "top_p": 1.0,
        "stream": False
    }

    try:
        print(f"[{datetime.now()}] 🧠 Відправка запиту на Рефлексію до Kimi 2.6...")
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        content = data["choices"][0]["message"]["content"]
        
        # Parse JSON from content (strip markdown if any)
        content = content.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(content)
        return parsed
        
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Помилка Reflection Agent: {e}")
        return {
            "regime": "UNKNOWN",
            "reasoning": f"Error calling API: {e}",
            "recommendation": "Emergency pause recommended."
        }

if __name__ == "__main__":
    test_context = (
        "Trade 1: LONG BTC/USDT. Entry: 64000, SL: 63800. Result: LOSS.\n"
        "Trade 2: LONG ETH/USDT. Entry: 3100, SL: 3050. Result: LOSS.\n"
        "Trade 3: SHORT SOL/USDT. Entry: 145, SL: 148. Result: LOSS.\n"
        "Market Condition: High volatility, low volume, BTC chopping in 2% range."
    )
    res = ask_kimi_reflection(test_context)
    print("Reflection Result:", res)
