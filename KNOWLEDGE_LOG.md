# KNOWLEDGE_LOG

[WORKS]
- Чітке розділення на сигнальний шар (`*_core.py`) і execution шар (`*_engine.py`).
- Наявний HTF-фільтр, об’ємні/ATR-фільтри та базова багатофакторність у setup logic.
- Логування в SQLite + Telegram-канал дають трасованість подій.

[BROKEN]
- bybit_bot.py:185-250 — відсутній жорсткий cap risk_pct у виконанні ордера — HIGH.
- bybit_bot.py:252+ — не було daily/session drawdown guard у runtime циклі — HIGH.
- bybit_bot.py:229-236 — ризик дубль-ордера при швидких тригерах/ретраях — HIGH.
- racer_engine.py:129-164 — відсутній global DD stop для нових входів — HIGH.
- smc_bridge.py:45,80 — прямий доступ `data['type']`/`data['ticker']` міг падати на неповному payload — MEDIUM.
- Архітектурно: повноцінний reconnect/state recovery після рестарту бота відсутній — MEDIUM.

[FIXED]
- bybit_bot.py — додано retry wrapper `safe_api_call`, cap risk_pct<=1%, анти-дубль ордера, daily/session risk guards.
- racer_engine.py — додано max drawdown stop для блокування нових входів після критичної просадки.
- smc_bridge.py — безпечний парсинг payload + context-manager для SQLite транзакцій.

[SKIP]
- Не робив повний рефактор усіх модулів в async-архітектуру (поза рамками одного проходу).
- Не переписував Pine-скрипти повністю; зафіксовано аудиторні рекомендації у фінальному звіті.

[TODO]
- HIGH: Додати persisted position-state і resume-поведінку після рестарту.
- HIGH: Додати централізований risk manager (max concurrent positions, exposure per sector).
- MEDIUM: Валідувати всі Bybit коди помилок та класифікувати retryable/non-retryable.
- MEDIUM: Додати integration-тести з моками ccxt/Bybit.
- LOW: Розширити Pine webhook schema validation (pydantic).

## SESSION: DEMO INTEGRATION + 2026 UPDATE
Дата: 2026-05-24

[WORKS]
- В `bybit_bot.py` додано явний demo/live перемикач через `use_demo` + `base_url` з конфігу.
- Бот тепер пріоритезує ключі з `active_config.json` і fallback на ENV.
- Додано окремий `test_demo_connection.py` для end-to-end smoke тесту API.

[BROKEN]
- Demo connectivity test у поточному середовищі: `GET https://api-demo.bybit.com/v5/market/time` не пройшов (мережевий/endpoint доступ недоступний з runtime) — HIGH.
- `active_config.json` не мав явних `use_demo/base_url/api_key/api_secret` — MEDIUM.

[FIXED]
- `active_config.json` оновлено: `use_demo=true`, `base_url=https://api-demo.bybit.com`, додані поля `api_key/api_secret`.
- `bybit_bot.py` оновлено на жорсткий endpoint selection: demo => `api-demo.bybit.com`, live => `api.bybit.com`.
- Додано `test_demo_connection.py` з 6 кроками перевірки (balance/ohlcv/order/status/cancel).

[SOURCES 2025-2026]
- Bybit API Docs — Demo Trading Service: https://bybit-exchange.github.io/docs/v5/demo
- Bybit API Docs — FAQ (demo vs testnet key/env matching): https://bybit-exchange.github.io/docs/faq
- Bybit Help Center — FAQ Demo Trading (порівняння Demo vs Testnet): https://www.bybit.com/en/help-center/article/FAQ-Demo-Trading
- arXiv (May 2026) Structural Limits of OHLCV Intraday Signals: https://arxiv.org/abs/2605.04004
- arXiv (Feb 2026) Walk-forward optimization on intraday crypto: https://arxiv.org/abs/2602.10785

[TODO]
- Перевірити доступність `api-demo.bybit.com` з production/vps мережі, де працюватиме бот.
- Додати fallback-діагностику DNS/TLS у test script для швидкого root cause.
- Додати friction model (fees+funding) у backtest pipeline.

## SESSION: FIX DEAD FILTERS — 2355 DRY CYCLES
Дата: 2026-05-24

[WORKS]
- Базова сигнальна логіка Racer працює, але блокується фільтрами тренду/імпульсу.

[BROKEN]
- Фіксований ADX-поріг робив sideway-блок навіть коли ринок близько до тренду.
- Heartbeat не пояснював причину блокування по конкретній парі/фільтру.
- Відправка ордерів не мала явного `dry_run` гейту.

[FIXED]
- Реалізовано адаптивний ADX поріг у `racer_core.py`:
  `adx_threshold = max(adx_min, avg(adx,last_n)*adx_adaptive_factor)`.
- Додано метрики `adx_threshold`, `rel_vol`, `fvg_size_atr` у `RacerBar` для діагностики.
- Оновлено heartbeat у `bybit_bot.py`: показує пару з найближчим сетапом + breakdown ADX/VOL/FVG vs пороги.
- Додано `dry_run=true` у конфіг і блокування реальних demo-ордерів в dry-run режимі.
- Знижено тестові пороги: `adx_min=12`, `vol_multiplier_min=0.8`, `fvg_min_size=0.08`.

[SOURCES 2025-2026]
- Bybit V5 API docs: https://bybit-exchange.github.io/docs/v5/intro
- Bybit V5 Demo Trading Service: https://bybit-exchange.github.io/docs/v5/demo
- Crypto/ADX practitioner notes 2025 (range regimes часто ADX 12–20):
  https://www.tradingview.com/ideas/search/adx%20crypto/

[TODO]
- Перевірити прод-логи Render після 2-3 циклів: чи з’явились перші valid setups.
- Якщо знов 0 — додати окрему range-логіку (варіант В) замість тільки threshold адаптації.
