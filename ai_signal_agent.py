import json
import os
import time
from datetime import datetime, timezone

from openai import OpenAI


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def generate_ai_signal(exchange, symbols, timeframe="15m"):
    """LLM advisory signal over a small liquid subset. Returns dict or None."""
    api_key = os.getenv("NVIDIA_API_KEY")
    base_url = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    model = os.getenv("NVIDIA_MODEL", "minimaxai/minimax-m2.7")

    if not api_key:
        return None

    sample_symbols = symbols[: min(len(symbols), 12)]
    market_snap = []
    for s in sample_symbols:
        try:
            t = exchange.fetch_ticker(s)
            market_snap.append(
                {
                    "symbol": s,
                    "last": _safe_float(t.get("last")),
                    "change_pct": _safe_float(t.get("percentage")),
                    "quote_volume": _safe_float(t.get("quoteVolume")),
                }
            )
        except Exception:
            continue

    if not market_snap:
        return None

    client = OpenAI(base_url=base_url, api_key=api_key)

    system = (
        "You are a crypto signal assistant. Return ONLY valid JSON with keys: "
        "symbol, direction, confidence, rationale, entry_hint, stop_hint. "
        "direction must be LONG, SHORT, or NONE. confidence 0..1."
    )
    user = {
        "time": str(datetime.now(timezone.utc)),
        "timeframe": timeframe,
        "market": market_snap,
    }

    def call_ai_api_with_retry(max_retries=3):
        for attempt in range(max_retries):
            try:
                return client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                    ],
                    temperature=0.2,
                    top_p=0.9,
                    max_tokens=400,
                    timeout=15.0,  # 15 seconds timeout
                )
            except Exception as e:
                if "429" in str(e):
                    wait = (2 ** attempt) * 10
                    print(f"[AI Signal Agent] AI API rate limit, чекаємо {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[AI Signal Agent] AI API помилка: {e}")
                    return None
        return None

    try:
        resp = call_ai_api_with_retry(max_retries=3)
        if resp is None:
            return None
        content = resp.choices[0].message.content if resp.choices and resp.choices[0].message else None
        if content is None:
            print("[AI Signal Agent] AI API повернув None")
            return None
            
        text = content.strip()
        
        # Robust Markdown JSON code blocks cleaning
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
            
        # Robust Regex JSON object extraction
        import re
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
            
        data = json.loads(text)
        if data.get("direction") not in {"LONG", "SHORT", "NONE"}:
            return None
        return data
    except Exception as e:
        print(f"[AI Signal Agent] Помилка генерації AI-сигналу: {e}")
        return None
