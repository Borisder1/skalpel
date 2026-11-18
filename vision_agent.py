import os
import io
import base64
import requests
import pandas as pd
from datetime import datetime

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "")

_PROVIDER_STATE = {
    "gemini_checked": False,
    "gemini_model": None,
    "groq_checked": False,
    "groq_model": None,
}


def _find_gemini_model() -> str:
    if _PROVIDER_STATE["gemini_checked"]:
        return _PROVIDER_STATE["gemini_model"]
    _PROVIDER_STATE["gemini_checked"] = True
    if not GEMINI_API_KEY:
        return None
    try:
        r = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}",
            timeout=10,
        )
        if r.status_code == 200:
            models = r.json().get("models", [])
            for m in models:
                name = m.get("name", "").replace("models/", "")
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods and ("flash" in name or "pro" in name):
                    _PROVIDER_STATE["gemini_model"] = name
                    print(f"[{datetime.now()}] 👁️ Знайдено доступну Gemini Vision модель: {name}")
                    return name
    except Exception:
        pass
    return None


def _find_groq_model() -> str:
    if _PROVIDER_STATE["groq_checked"]:
        return _PROVIDER_STATE["groq_model"]
    _PROVIDER_STATE["groq_checked"] = True
    if not GROQ_API_KEY:
        return None
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        r = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=10)
        if r.status_code == 200:
            for m in r.json().get("data", []):
                cid = m.get("id", "")
                if any(k in cid.lower() for k in ("vision", "pixtral", "llava")):
                    # Перевіримо, чи модель реально відповідає (не 404)
                    _PROVIDER_STATE["groq_model"] = cid
                    return cid
    except Exception:
        pass
    return None


def _has_active_vision_provider() -> bool:
    if _find_gemini_model():
        return True
    if _find_groq_model():
        return True
    if OPENROUTER_API_KEY:
        return True
    return False


def _ask_gemini_vision(b64_image: str) -> str:
    model_name = _find_gemini_model()
    if not model_name:
        return "NEUTRAL"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [
                {"text": "You are an expert Quant Trader. Look at this candlestick chart. Reply with one word: BULLISH, BEARISH, or NEUTRAL."},
                {"inline_data": {"mime_type": "image/png", "data": b64_image}},
            ]
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 10},
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            text = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip().upper()
            if "BULL" in text:
                return "BULLISH"
            if "BEAR" in text:
                return "BEARISH"
        elif resp.status_code in (400, 403, 404):
            _PROVIDER_STATE["gemini_model"] = None
    except Exception:
        pass
    return "NEUTRAL"


def _ask_groq_vision(b64_image: str) -> str:
    model = _find_groq_model()
    if not model:
        return "NEUTRAL"
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this candlestick chart. Reply with one word: BULLISH, BEARISH, or NEUTRAL."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}},
            ],
        }],
        "max_tokens": 10,
        "temperature": 0.1,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"].strip().upper()
            if "BULL" in text:
                return "BULLISH"
            if "BEAR" in text:
                return "BEARISH"
        elif resp.status_code in (400, 403, 404):
            # Відключаємо неробочу модель, щоб не витрачати RAM на генерацію графіків
            _PROVIDER_STATE["groq_model"] = None
    except Exception:
        pass
    return "NEUTRAL"


def ask_vision_oracle(df: pd.DataFrame, symbol: str) -> str:
    """
    Перевіряє наявність робочої Vision AI моделі ПЕРЕД імпортом matplotlib (економія ~110MB RAM на Render).
    """
    if df is None or df.empty or len(df) < 10:
        return "NEUTRAL"

    if not _has_active_vision_provider():
        return "NEUTRAL"

    buf = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import mplfinance as mpf

        buf = io.BytesIO()
        mc = mpf.make_marketcolors(up="g", down="r", edge="inherit", wick="inherit", volume="in")
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle="", facecolor="white")

        plot_df = df.tail(50).copy()
        if not isinstance(plot_df.index, pd.DatetimeIndex):
            plot_df.index = pd.to_datetime(plot_df["timestamp"], unit="ms")
        plot_df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}, inplace=True)

        mpf.plot(plot_df, type="candle", style=s, volume=True, savefig=dict(fname=buf, dpi=72, bbox_inches="tight", pad_inches=0.1))
        plt.close("all")
        buf.seek(0)
        b64_image = base64.b64encode(buf.read()).decode("utf-8")

        if _PROVIDER_STATE["gemini_model"]:
            res = _ask_gemini_vision(b64_image)
            if res != "NEUTRAL":
                return res
        if _PROVIDER_STATE["groq_model"]:
            res = _ask_groq_vision(b64_image)
            if res != "NEUTRAL":
                return res
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"
    finally:
        try:
            if buf:
                buf.close()
        except Exception:
            pass
