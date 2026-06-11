import requests
import json
from datetime import datetime

NVIDIA_API_KEY = "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"

def ask_kimi(prompt: str) -> str:
    invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "moonshotai/kimi-k2.6",
        "messages": [
            {"role": "system", "content": "You are an expert Quant Trader and Financial Analyst."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 1024,
        "temperature": 0.7,
        "top_p": 1.0,
        "stream": False,
        "chat_template_kwargs": {"thinking": True},
    }

    print(f"[{datetime.now()}] 🚀 Sending request to Kimi 2.6...")
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
    test_prompt = "I just lost 3 long trades in a row on BTC/USDT. The market was dumping but my SMC indicator showed a bullish Fair Value Gap. Why did my indicator fail?"
    answer = ask_kimi(test_prompt)
    print("\n--- Kimi's Response ---")
    print(answer)
