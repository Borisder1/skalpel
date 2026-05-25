# FINAL_AUDIT_REPORT

## 1) Загальна оцінка проєкту: 6.8/10
- Якість коду: 7.0/10
- Якість стратегії: 6.5/10
- Безпека депозиту: 6.0/10 (після цього проходу покращено)

## 2) TOP-3 критичні знахідки
1. Відсутність runtime daily/session loss guards у live-циклі (`bybit_bot.py`).
2. Ризик дублювання ордерів і нестабільна обробка API-rate limit в execution path.
3. Відсутність max drawdown stop для нових входів у `racer_engine.py`.

## 3) Що виправлено
- Додано ризик-гарди: `MAX_DAILY_LOSS_PCT`, `MAX_SESSION_DRAWDOWN_PCT`.
- Додано `safe_api_call()` з retry/backoff для API помилок.
- Додано анти-дубль ордера (`MIN_ORDER_INTERVAL_SEC`).
- Додано DD-stop у `racer_engine.py`.
- Виправлено безпечність webhook ingest у `smc_bridge.py`.

## 4) Реалістичний прогноз
- Потенційно прибуткова тільки у трендові/імпульсні фази з достатньою ліквідністю.
- У флeті та при новинних шпильках стратегія вразлива (false setup + slippage).
- Реалістичний win-rate після фільтрів: ~38-52% (залежить від ринку/комісій/slippage).

## 5) Наступні кроки (пріоритет)
1. [HIGH] Додати portfolio-level risk (max exposure, max open trades, correlation cap).
2. [HIGH] Реальний forward-test 4-8 тижнів з повним friction model.
3. [HIGH] Walk-forward/rolling re-optimization без leakage.
4. [MEDIUM] Уніфікувати Python/Pine правила входу/виходу 1:1.
5. [MEDIUM] Повний набір unit/integration тестів execution/risk.

## 6) Актуальні джерела 2024–2025 (методи/ринковий контекст)
- NBER, *Trading Volume Alpha* (2024-09, working paper): об’ємні сигнали дають альфу, але чутливі до витрат виконання.
- Kellogg (2024), *Day Traders, Noise, and Market Makers*: активний intraday-ритейл системно програє без структурного edge/ліквідності.
- ScienceDirect (2025), *Dumb money? Social network attention...*: поведінкові/натовпні сигнали нестабільні та regime-dependent.

Висновок: SMC/FVG може працювати лише з жорстким risk-control, фільтрами режиму ринку та реалістичним friction model.
