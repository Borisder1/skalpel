import json
import os
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

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0.2,
            top_p=0.9,
            max_tokens=400,
        )
        text = resp.choices[0].message.content.strip()
        data = json.loads(text)
        if data.get("direction") not in {"LONG", "SHORT", "NONE"}:
            return None
        return data
    except Exception as e:
        print(f"[AI Signal Agent] Помилка генерації AI-сигналу: {e}")
        return None
