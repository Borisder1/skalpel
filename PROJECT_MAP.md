# PROJECT_MAP

## Файли (1 речення)
- `active_config.json`: runtime-конфіг параметрів фільтрів/ризику для сканера.
- `engine.py`: бектест-двигун SMC Agent v6 з трекінгом трейдів та equity.
- `smc_core.py`: генерація `BarState` (SMC сигнали, структури, FVG, HTF-тренд).
- `racer_core.py`: швидша сигнальна логіка Racer (setup generation).
- `racer_engine.py`: симуляція виконання setup’ів Racer з TP/SL.
- `ai_signal_agent.py`: advisory AI-сигнали як fallback при сухому ринку.
- `cooperative_agents.py`: мульти-агентна діагностика патернів, статистики, оптимізації.
- `bybit_bot.py`: основний live/demo-бот (скан, сигнал, ордер, логи, heartbeat).
- `data_fetcher.py`: завантаження/підготовка OHLCV для тестів.
- `db_logger.py`: SQLite-логування угод і оновлення їх статусів.
- `telegram_notifier.py`: доставка повідомлень у Telegram.
- `smc_bridge.py`: FastAPI webhook-міст для Pine/діагностики в SQLite.
- `run_backtest.py`: CLI entry-point для бектестів та оптимізації.
- `optimizer.py`: grid-search і walk-forward для SMC/Racer.
- `report.py`: агрегування/вивід результатів тестів.
- `requirements.txt`: залежності Python-проєкту.
- `DIAGNOSTIC_REPORT.md`: історичний звіт діагностики.
- `COMPLETE_ANALYSIS.md`: попередній широкий аналіз системи.
- `ACTION_PLAN.md`: roadmap виправлень/покращень.
- `SKRYPT V6/SMC_Agent_v6.pine`: основна Pine v6 стратегія SMC Agent.
- `SKRYPT V6/SMC_Racer_v1.pine`: Pine v6 реалізація Racer-логіки.
- `PINE_FIXES.pine`: фрагменти критичних Pine-виправлень.
- `PINE_FIXES_NAKED_FVG.pine`: фрагменти оптимізації під Naked FVG.

## Dependency graph (текст)
`run_backtest.py` -> (`data_fetcher.py`, `smc_core.py`/`racer_core.py`, `engine.py`/`racer_engine.py`, `optimizer.py`, `report.py`).

`bybit_bot.py` -> (`racer_core.py`, `db_logger.py`, `telegram_notifier.py`, `ai_signal_agent.py`, `active_config.json`, Bybit via ccxt).

`cooperative_agents.py` -> (`racer_core.py`, `optimizer.py`, `db_logger.py`, `telegram_notifier.py`, SQLite).

`smc_bridge.py` <- webhook from Pine alerts; writes to `smc_diagnostics.db`.

## Entry points
- `bybit_bot.py::run_bot()` — автономний demo/live цикл.
- `run_backtest.py` (`__main__`) — CLI бектести/оптимізація.
- `smc_bridge.py` (`uvicorn`) — webhook API `/webhook`.

## Де живуть сигнали (pipeline)
1. OHLCV -> `racer_core.py::analyze_racer` / `smc_core.py::analyze`.
2. Сформований setup/state -> `bybit_bot.py` (live) або `*_engine.py` (backtest).
3. Live: `bybit_bot.py` -> `execute_demo_order()` -> Bybit API.
4. Паралельно: логи у SQLite через `db_logger.py`, нотифікації в `telegram_notifier.py`.
5. Pine сигнали: TradingView alert JSON -> `smc_bridge.py` -> `smc_diagnostics.db`.
