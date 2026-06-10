# Local Bot Regime Automation

This repo includes a small local control plane for switching Freqtrade bots between bearish, neutral, and bullish market regimes.

The key idea is:
- `user_data/regime_spawn_rules.json` decides which containers should be active for each regime.
- `scripts/regime_controller.py` detects the current regime and starts or stops managed containers.
- `user_data/bots_registry.json` is the manifest used by the reporting and optimization scripts.

## Files

- `scripts/regime_controller.py`
- `scripts/regime_controller_cron.sh`
- `scripts/registry_iterative_optimize.py`
- `scripts/freqtrade_profit_report.py`
- `scripts/freqtrade_health_report.py`
- `setup-freqtrade-lab.zsh`
- `user_data/bots_registry.json`
- `user_data/regime_spawn_rules.json`
- `user_data/regime_controller_state.json`

## Regime Controller

`scripts/regime_controller.py` reads market data, calculates a simple regime signal, and then compares the result against `user_data/regime_spawn_rules.json`.

The controller uses:
- BTC futures market data from `user_data/data/binance/futures/BTC_USDT_USDT-1h-futures.feather`
- 30 day and 120 day returns
- a short/long EMA trend check
- confirmation rules for consecutive detections
- a cooldown window between switches

It writes run state to `user_data/regime_controller_state.json`, including:
- last detected regime
- last target regime
- last action list
- last metrics snapshot

Run modes:
- `dry-run` prints what would change
- `execute` actually starts or stops containers

The cron wrapper `scripts/regime_controller_cron.sh` adds a lock so only one regime-controller run executes at a time.

## Regime Rules

`user_data/regime_spawn_rules.json` is the file that tells the controller what to do for each regime.

It contains:
- thresholds used to classify bearish, neutral, and bullish conditions
- confirmation settings
- containers managed by the controller
- the containers to activate in each regime
- optional compose metadata for bots that need to be spawned with `docker compose up -d`

If you add a bot that should be switched by regime, update this file first.

## Registry

`user_data/bots_registry.json` is the manifest used by the reporting and optimization scripts.

It should be updated when:
- a bot name changes
- a config file changes
- a listen port changes
- a bot is added or removed
- the bot status changes from running to stopped, or the reverse

The registry is used by:
- `scripts/freqtrade_profit_report.py`
- `scripts/freqtrade_health_report.py`
- `scripts/registry_iterative_optimize.py`

If the registry is stale, the reports will miss bots or point at the wrong API port.

## Reporting Commands

`setup-freqtrade-lab.zsh` exposes shell helpers:
- `CB` for the results report
- `CBS` for the ranked results report
- `CBH` for the health report

These helpers are thin wrappers around the Python scripts and keep the reporting workflow consistent.

## Optimization Flow

`scripts/registry_iterative_optimize.py` discovers running registry bots, runs backtests, and writes snapshots for the best-performing variants.

Outputs include:
- `optimization_results.csv`
- `optimization_results.json`
- per-bot best strategy snapshots
- `best_config.json` for the winning profile

This script depends on the registry being current, because it uses the registry to map bots to config files and strategies.

## Update Checklist

When you add or change a bot:
- update `user_data/bots_registry.json`
- update `user_data/regime_spawn_rules.json` if the bot is part of regime switching
- update the compose file if the container name or start method changed
- update the config file if the listen port or strategy changed
- re-run `CBH` and `CBS --summary-only` to verify the registry still matches the running containers
