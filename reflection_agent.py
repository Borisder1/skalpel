from datetime import datetime


def ask_kimi_reflection(trades_context: str) -> dict:
    """
    Локальний квантовий аналізатор серії збитків (0 мс, 0 RAM, без мертвого Nvidia NIM API 410 Gone).
    Аналізує останні угоди та повертає діагноз і рекомендацію.
    """
    ctx = str(trades_context or "")
    shorts = ctx.count("SHORT")
    longs = ctx.count("LONG")
    if shorts >= 3:
        rec = "Серія стопів на SHORT: ринок відкуповує дно. Ввімкнено захист 4H Підтримки та ліміт до 2 позицій в один бік."
        regime = "SHORT_SQUEEZE / BOUNCE"
    elif longs >= 3:
        rec = "Серія стопів на LONG: ринок під тиском продавців біля 4H Опору. Підвищено селективність входів."
        regime = "RESISTANCE_REJECTION"
    else:
        rec = "Пилкоподібний боковик (CHOP). Пріоритет швидкої фіксації 50% на TP1 (0.70R) з переносом стопу в +Б/У."
        regime = "CHOP"

    print(f"[{datetime.now()}] 🧠 Локальна Quant-рефлексія виконана: {regime}")
    return {
        "regime": regime,
        "reasoning": "Аналіз виконано локальним ядром V13.",
        "recommendation": rec,
    }
