"""
Квантове Ядро (Quantitative Scoring Engine)
============================================
Локальний математичний модуль для миттєвої оцінки якості торгових сигналів.
Замінює зовнішній NVIDIA AI — працює за 0ms, без API, без таймаутів.

Оцінює кожен сигнал по 8 зважених факторах та повертає score 0.0-1.0.
Підтримує самонавчання: аналізує WIN/LOSS угоди і коригує ваги.
"""

import json
import os
import math
import time
import requests
from datetime import datetime

# Файл для збереження оптимізованих ваг
WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quant_weights.json")

# Кеш для макроекономічних показників (DXY, Crude Oil, Gold)
MACRO_CACHE = {
    "data": None,
    "last_fetched": 0.0
}

# Дефолтні ваги факторів (сума = 1.0)
DEFAULT_WEIGHTS = {
    "rr_quality":       0.18,   # Risk:Reward якість
    "volume_confirm":   0.12,   # Об'єм підтвердження
    "adx_strength":     0.12,   # ADX сила тренду
    "fvg_size":         0.08,   # FVG розмір
    "htf_confluence":   0.12,   # HTF конфлюенція
    "session_quality":  0.08,   # Якість торгової сесії
    "smc_structure":    0.08,   # SMC структурна конфлюенція
    "impulse_quality":  0.04,   # Імпульс свічки
    "macro_confluence": 0.18,   # Макро конфлюенція (DXY, Oil, Gold)
}


def _load_weights() -> dict:
    """Завантажує оптимізовані ваги або дефолтні."""
    if os.path.exists(WEIGHTS_FILE):
        try:
            with open(WEIGHTS_FILE, "r") as f:
                data = json.load(f)
                return data.get("weights", DEFAULT_WEIGHTS)
        except Exception:
            pass
    return DEFAULT_WEIGHTS.copy()


def _save_weights(weights: dict, stats: dict = None):
    """Зберігає оптимізовані ваги."""
    data = {
        "weights": weights,
        "updated_at": datetime.now().isoformat(),
        "stats": stats or {},
    }
    temp = WEIGHTS_FILE + ".tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, WEIGHTS_FILE)


def _sigmoid(x: float, center: float = 0.5, steepness: float = 10.0) -> float:
    """Сігмоїдне згладжування для плавних переходів."""
    try:
        return 1.0 / (1.0 + math.exp(-steepness * (x - center)))
    except OverflowError:
        return 0.0 if x < center else 1.0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# ─── ФАКТОРИ ОЦІНКИ ────────────────────────────────────────────

def _score_rr_quality(entry: float, sl: float, tp1: float, tp2: float, direction: str) -> float:
    """Оцінює якість Risk:Reward."""
    if direction == "LONG":
        risk = max(entry - sl, 1e-10)
        reward1 = tp1 - entry
        reward2 = tp2 - entry
    else:
        risk = max(sl - entry, 1e-10)
        reward1 = entry - tp1
        reward2 = entry - tp2

    rr1 = reward1 / risk
    rr2 = reward2 / risk

    # RR1 >= 1.5 і RR2 >= 2.5 = ідеальний сетап
    score1 = _clamp(rr1 / 2.0)          # 0-2 RR → 0.0-1.0
    score2 = _clamp(rr2 / 4.0)          # 0-4 RR → 0.0-1.0
    return _clamp(score1 * 0.4 + score2 * 0.6)


def _score_volume(rel_vol: float, vol_threshold: float) -> float:
    """Оцінює підтвердження об'ємом."""
    if rel_vol <= 0:
        return 0.0
    # rel_vol = 1.0 = середній, 2.0 = подвійний
    ratio = rel_vol / max(vol_threshold, 0.1)
    return _clamp(_sigmoid(ratio, center=1.0, steepness=3.0))


def _score_adx(adx_value: float, adx_threshold: float) -> float:
    """Оцінює силу тренду через ADX."""
    if adx_value <= 0 or math.isnan(adx_value):
        return 0.0
    # ADX > threshold = тренд. Чим більше — тим краще
    excess = adx_value - adx_threshold
    if excess < 0:
        return _clamp(0.2 + 0.3 * (adx_value / max(adx_threshold, 1)))
    # excess 0-20 → 0.5-1.0
    return _clamp(0.5 + excess / 40.0)


def _score_fvg_size(fvg_size_atr: float, fvg_min: float) -> float:
    """Оцінює розмір FVG відносно ATR."""
    if fvg_size_atr <= 0:
        return 0.0
    ratio = fvg_size_atr / max(fvg_min, 0.01)
    return _clamp(_sigmoid(ratio, center=1.0, steepness=4.0))


def _score_htf_confluence(is_htf_aligned: bool, direction: str, is_htf_bullish: bool, is_htf_bearish: bool) -> float:
    """Оцінює конфлюенцію з HTF трендом."""
    if direction == "LONG" and is_htf_bullish:
        return 1.0
    elif direction == "SHORT" and is_htf_bearish:
        return 1.0
    elif (direction == "LONG" and is_htf_bearish) or (direction == "SHORT" and is_htf_bullish):
        return 0.1  # Проти HTF тренду = дуже слабкий сигнал
    return 0.4  # Нейтральний HTF


def _score_session(session: str) -> float:
    """Оцінює якість торгової сесії."""
    session_scores = {
        "London": 0.9,
        "NewYork": 1.0,
        "LondonNewYork": 1.0,  # Перетин сесій = найкращий час
        "Tokyo": 0.6,
        "Sydney": 0.4,
        "Off": 0.3,
    }
    return session_scores.get(session, 0.3)


def _score_smc_structure(bos_aligned: bool, choch_aligned: bool, ob_aligned: bool, fvg_naked: bool) -> float:
    """Оцінює якість SMC структури."""
    score = 0.0
    if bos_aligned:
        score += 0.35
    if choch_aligned:
        score += 0.25
    if ob_aligned:
        score += 0.25
    if fvg_naked:
        score += 0.15
    return _clamp(score)


def _score_impulse(is_impulse: bool, body_to_atr_ratio: float) -> float:
    """Оцінює якість імпульсної свічки."""
    if not is_impulse:
        return 0.2
    return _clamp(0.5 + body_to_atr_ratio * 0.3)


# ─── МАКРО-КОНФЛЮЕНЦІЯ (Correlation) ──────────────────────────

def get_macro_context() -> dict:
    """Отримує макроекономічний контекст з Yahoo Finance із кешуванням на 10 хвилин."""
    now = time.time()
    if MACRO_CACHE["data"] and (now - MACRO_CACHE["last_fetched"] < 600):
        return MACRO_CACHE["data"]

    headers = {"User-Agent": "Mozilla/5.0"}
    assets = {
        "DXY": "DX-Y.NYB",
        "Oil": "CL=F",
        "Gold": "GC=F"
    }
    
    result_data = {}
    for name, ticker in assets.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1h"
        try:
            r = requests.get(url, headers=headers, timeout=5)
            data = r.json()
            res = data.get("chart", {}).get("result")
            if res:
                quotes = res[0]["indicators"]["quote"][0]
                closes = [c for c in quotes.get("close", []) if c is not None]
                if len(closes) >= 2:
                    current_price = closes[-1]
                    sma = sum(closes) / len(closes)
                    trend = "BULL" if current_price > sma else "BEAR"
                    result_data[name] = trend
                else:
                    result_data[name] = "NEUTRAL"
            else:
                result_data[name] = "NEUTRAL"
        except Exception:
            result_data[name] = "NEUTRAL"
            
    MACRO_CACHE["data"] = result_data
    MACRO_CACHE["last_fetched"] = now
    return result_data


def _score_macro_confluence(direction: str) -> float:
    """Оцінює макро-кореляцію (DXY, Oil, Gold) залежно від напрямку."""
    macro = get_macro_context()
    dxy = macro.get("DXY", "NEUTRAL")
    oil = macro.get("Oil", "NEUTRAL")
    gold = macro.get("Gold", "NEUTRAL")
    
    score = 0.0
    
    # 1. DXY (вага 40%): Слабкий DXY = Bullish для BTC
    if direction == "LONG":
        if dxy == "BEAR": score += 0.4
        elif dxy == "NEUTRAL": score += 0.2
    else:
        if dxy == "BULL": score += 0.4
        elif dxy == "NEUTRAL": score += 0.2
        
    # 2. Нафта (вага 30%): Падіння нафти = дефляційний імпульс (Bullish)
    if direction == "LONG":
        if oil == "BEAR": score += 0.3
        elif oil == "NEUTRAL": score += 0.15
    else:
        if oil == "BULL": score += 0.3
        elif oil == "NEUTRAL": score += 0.15
        
    # 3. Золото (вага 30%): Ріст золота = захист від девальвації (Bullish для крипти як цифрового золота)
    if direction == "LONG":
        if gold == "BULL": score += 0.3
        elif gold == "NEUTRAL": score += 0.15
    else:
        if gold == "BEAR": score += 0.3
        elif gold == "NEUTRAL": score += 0.15
        
    return score


# ─── ГОЛОВНА ФУНКЦІЯ СКОРИНГУ ──────────────────────────────────

def score_setup(
    # Ціни сетапу
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    direction: str,
    # Технічні дані з RacerBar
    adx: float = 0.0,
    adx_threshold: float = 12.0,
    rel_vol: float = 0.0,
    vol_threshold: float = 0.7,
    fvg_size_atr: float = 0.0,
    fvg_min: float = 0.08,
    atr: float = 0.0,
    # SMC/HTF дані
    is_htf_bullish: bool = False,
    is_htf_bearish: bool = False,
    session: str = "Off",
    bos_bull: bool = False,
    bos_bear: bool = False,
    choch_bull: bool = False,
    choch_bear: bool = False,
    ob_active: bool = False,
    bull_fvg: bool = False,
    bear_fvg: bool = False,
    is_impulse_bull: bool = False,
    is_impulse_bear: bool = False,
    # Додатково
    symbol: str = "",
) -> dict:
    """
    Миттєво оцінює якість сетапу по 8 факторах.
    
    Returns:
        {
            "score": float (0.0-1.0),
            "factors": dict з індивідуальними оцінками,
            "verdict": str ("AUTO_EXECUTE" | "MANUAL_CONFIRM" | "SKIP"),
            "rationale": str (людсько-зрозуміле пояснення),
        }
    """
    weights = _load_weights()

    # Визначаємо вирівнювання SMC з напрямком
    is_long = direction == "LONG"
    bos_aligned = bos_bull if is_long else bos_bear
    choch_aligned = choch_bull if is_long else choch_bear
    fvg_naked = bull_fvg if is_long else bear_fvg
    is_impulse = is_impulse_bull if is_long else is_impulse_bear
    
    body_to_atr = abs(entry - sl) / max(atr, 1e-10) if atr > 0 else 0.5

    # Рахуємо всі 9 факторів
    factors = {
        "rr_quality":       _score_rr_quality(entry, sl, tp1, tp2, direction),
        "volume_confirm":   _score_volume(rel_vol, vol_threshold),
        "adx_strength":     _score_adx(adx, adx_threshold),
        "fvg_size":         _score_fvg_size(fvg_size_atr, fvg_min),
        "htf_confluence":   _score_htf_confluence(True, direction, is_htf_bullish, is_htf_bearish),
        "session_quality":  _score_session(session),
        "smc_structure":    _score_smc_structure(bos_aligned, choch_aligned, ob_active, fvg_naked),
        "impulse_quality":  _score_impulse(is_impulse, body_to_atr),
        "macro_confluence": _score_macro_confluence(direction),
    }

    # Зважена сума
    total_score = sum(factors[k] * weights.get(k, 0.0) for k in factors)
    total_score = _clamp(total_score)

    # Вердикт
    if total_score >= 0.65:
        verdict = "AUTO_EXECUTE"
    elif total_score >= 0.40:
        verdict = "MANUAL_CONFIRM"
    else:
        verdict = "SKIP"

    # Генеруємо rationale
    top_factors = sorted(factors.items(), key=lambda x: x[1], reverse=True)[:3]
    weak_factors = sorted(factors.items(), key=lambda x: x[1])[:2]
    
    factor_names_ua = {
        "rr_quality": "R:R",
        "volume_confirm": "Об'єм",
        "adx_strength": "ADX тренд",
        "fvg_size": "FVG",
        "htf_confluence": "HTF конфлюенція",
        "session_quality": "Сесія",
        "smc_structure": "SMC структура",
        "impulse_quality": "Імпульс",
        "macro_confluence": "Макро тренд",
    }
    
    strong = ", ".join(f"{factor_names_ua.get(k, k)}={v:.0%}" for k, v in top_factors)
    weak = ", ".join(f"{factor_names_ua.get(k, k)}={v:.0%}" for k, v in weak_factors)
    
    rationale = f"Score {total_score:.0%} | Сильні: {strong} | Слабкі: {weak}"

    return {
        "score": round(total_score, 4),
        "factors": {k: round(v, 4) for k, v in factors.items()},
        "verdict": verdict,
        "rationale": rationale,
    }


# ─── САМОНАВЧАННЯ ──────────────────────────────────────────────

def learn_from_trade(factors_snapshot: dict, outcome: str, pnl: float):
    """
    Оновлює ваги на основі результату угоди.
    
    factors_snapshot: dict з оцінками факторів на момент входу
    outcome: "WIN" або "LOSS"
    pnl: реальний PnL в USDT
    """
    weights = _load_weights()
    
    # Швидкість навчання
    lr = 0.02 if outcome == "WIN" else 0.015
    
    for factor_name, factor_score in factors_snapshot.items():
        if factor_name not in weights:
            continue
        
        if outcome == "WIN":
            # Збільшуємо вагу факторів з високим score у виграшних угодах
            if factor_score > 0.6:
                weights[factor_name] += lr * factor_score
            # Зменшуємо вагу факторів з низьким score у виграшних
            elif factor_score < 0.3:
                weights[factor_name] -= lr * 0.5
        else:
            # Зменшуємо вагу факторів з високим score у програшних
            # (якщо фактор був "впевнений", але угода програла — він ненадійний)
            if factor_score > 0.6:
                weights[factor_name] -= lr * 0.3
            # Збільшуємо вагу факторів з низьким score у програшних
            # (можливо, ми ігнорували важливий сигнал)
            elif factor_score < 0.3:
                weights[factor_name] += lr * 0.2
    
    # Нормалізуємо ваги: мінімум 0.02, сума = 1.0
    for k in weights:
        weights[k] = max(0.02, weights[k])
    total = sum(weights.values())
    weights = {k: v / total for k, v in weights.items()}
    
    # Рахуємо статистику
    stats_file = WEIGHTS_FILE
    stats = {}
    if os.path.exists(stats_file):
        try:
            with open(stats_file) as f:
                stats = json.load(f).get("stats", {})
        except Exception:
            pass
    
    stats["total_learned"] = stats.get("total_learned", 0) + 1
    stats["wins_learned"] = stats.get("wins_learned", 0) + (1 if outcome == "WIN" else 0)
    stats["losses_learned"] = stats.get("losses_learned", 0) + (1 if outcome == "LOSS" else 0)
    stats["last_learn"] = datetime.now().isoformat()
    
    _save_weights(weights, stats)
    print(f"[QuantEngine] 🧠 Навчання: {outcome} PnL={pnl:+.2f} | Оновлено ваги ({stats['total_learned']} угод)")


if __name__ == "__main__":
    # Тест: оцінка фіктивного сетапу
    result = score_setup(
        entry=100.0, sl=98.0, tp1=103.0, tp2=105.0,
        direction="LONG",
        adx=25.0, adx_threshold=15.0,
        rel_vol=1.5, vol_threshold=0.7,
        fvg_size_atr=0.15, fvg_min=0.08,
        atr=2.0,
        is_htf_bullish=True, is_htf_bearish=False,
        session="NewYork",
        bos_bull=True, bos_bear=False,
        choch_bull=True, choch_bear=False,
        ob_active=True,
        bull_fvg=True, bear_fvg=False,
        is_impulse_bull=True, is_impulse_bear=False,
        symbol="BTC/USDT:USDT",
    )
    print(f"Score: {result['score']}")
    print(f"Verdict: {result['verdict']}")
    print(f"Rationale: {result['rationale']}")
    print(f"Factors: {json.dumps(result['factors'], indent=2)}")
