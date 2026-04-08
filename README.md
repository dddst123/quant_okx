# OKX Quant MVP

Optional Chinese version: [README.zh-CN.md](README.zh-CN.md)

A practical starting point for building and validating OKX spot automation. The repository includes a simple single-instrument bot, a multi-asset factor portfolio engine, backtesting and walk-forward tooling, market-state monitoring, and production-style operational safeguards.

## What is included

- `SMA` sample bot for learning the OKX API flow, signal generation, and basic execution controls.
- `VolumeTrendFactorStrategy` for cross-sectional spot rotation on the OKX `USDT` spot universe.
- Historical backtests and rolling walk-forward validation.
- Market-state observation and de-risking hooks.
- Stop-loss, drawdown circuit breaker, persistent state, log rotation, retries, alerts, and a long-running guardian daemon.
- Make targets for simulated runs, dry-run live checks, and quick research loops.

## Public repo vs private research

This repository is now set up so the public version only carries code, docs, and safe templates.

Keep these private and local:

- API keys and passphrases.
- Tuned factor parameters and market-state thresholds.
- Local validation reports you do not want to publish.
- Live or simulated runtime state.

The following paths are ignored by Git and are the right place for private data:

- `.env`
- `.env.simulated`
- `.env.live`
- `reports/`
- `logs/`
- `state/`

Tracked `*.example` env files intentionally contain only safe operational values. The runtime falls back to built-in defaults from `/Users/mac/project/quant_okx/okx_quant/config.py`, and you can override those defaults privately in your ignored local env files.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
cp .env.simulated.example .env.simulated
cp .env.live.example .env.live
```

Then fill credentials only in your local ignored profile file:

- `/Users/mac/project/quant_okx/.env.simulated` for OKX demo trading.
- `/Users/mac/project/quant_okx/.env.live` for OKX live trading.

## Environment loading order

Settings load in this order:

1. `/Users/mac/project/quant_okx/.env`
2. `OKX_ENV_FILE=/path/to/private.env` if you pass it at launch
3. `OKX_ENV_PROFILE=simulated` -> `/Users/mac/project/quant_okx/.env.simulated`
4. `OKX_ENV_PROFILE=live` -> `/Users/mac/project/quant_okx/.env.live`

Recommended layout:

- `/Users/mac/project/quant_okx/.env`: shared non-sensitive settings.
- `/Users/mac/project/quant_okx/.env.simulated`: demo keys plus your private demo profile.
- `/Users/mac/project/quant_okx/.env.live`: live keys plus your stricter live profile.

If you want to keep multiple local research profiles, create extra ignored env files and launch with `OKX_ENV_FILE=/absolute/path/to/private.env`.

## One-command Make targets

From `/Users/mac/project/quant_okx`:

```bash
make sim-market-state
make sim-once
make sim-run
make sim-guard-once
make sim-guard
make sim-dashboard
make live-dry-run
make live-once
make live-dashboard
make wf-quick
make backtest
```

What they do:

- `make sim-market-state`: inspect the current simulated market-state snapshot.
- `make sim-once`: run one simulated portfolio cycle.
- `make sim-run`: run the continuous simulated portfolio loop.
- `make sim-guard-once`: run one guardian tick.
- `make sim-guard`: run the long-lived guardian daemon.
- `make sim-dashboard`: serve the local dashboard on `http://127.0.0.1:8787`.
- `make live-dry-run`: use `/Users/mac/project/quant_okx/.env.live` but force `OKX_DRY_RUN=true`.
- `make live-once`: run one live-profile cycle using the current local live settings.
- `make live-dashboard`: serve the local dashboard for the live-profile log/output paths.
- `make wf-quick`: run a smaller walk-forward smoke test.
- `make backtest`: run the 1/2/3 year backtest batch.

## CLI commands

```bash
python -m okx_quant.main factors-market-state
python -m okx_quant.main factors-rank
python -m okx_quant.main factors-once
python -m okx_quant.main factors-run
python -m okx_quant.main factors-guard --once
python -m okx_quant.main factors-guard --max-loops 24
python -m okx_quant.main factors-dashboard --host 127.0.0.1 --port 8787
python -m okx_quant.main factors-backtest --years 1 2 3
python -m okx_quant.main factors-walk-forward --lookback-years 4 --train-days 365 --test-days 90 --step-days 90
python -m okx_quant.main factors-walk-forward --lookback-years 2 --train-days 240 --test-days 60 --step-days 120 --search-profile quick
python -m okx_quant.main factors-risk-reset
```

## Local dashboard

The dashboard serves a local-only page that reads guardian event logs and shows:

- realtime equity / NAV / drawdown curves from guardian ticks
- current holdings, picks, planned orders, and market-state reason
- the latest backtest summary found in `/Users/mac/project/quant_okx/reports/backtests`

Typical workflow:

1. Run the guardian in one terminal: `make sim-guard`
2. Run the dashboard in another terminal: `make sim-dashboard`
3. Open [http://127.0.0.1:8787](http://127.0.0.1:8787)

If the page shows no live data yet, the guardian has not written fresh ticks to the local log.

## Suggested validation path

Use a staged rollout instead of jumping straight to live capital:

1. Backtest the current private profile.
2. Run walk-forward validation to inspect out-of-sample behavior.
3. Forward test on OKX simulated trading.
4. Keep `OKX_DRY_RUN=true` for live-profile checks until logging, alerts, and guardian behavior look stable.
5. Only then consider a small live allocation.

## Safety notes

- Do not grant withdrawal permission to any trading API key.
- Keep live mode on dry-run until you have finished demo forward testing.
- Treat tuned parameters as private research assets, not public defaults.
- Review logs and guardian output before enabling real orders.

## Project layout

```text
okx_quant/
  alerts.py
  backtest.py
  bot.py
  client.py
  config.py
  factor_bot.py
  guardian.py
  logging_utils.py
  main.py
  market_state.py
  models.py
  portfolio_risk.py
  risk.py
  universe.py
  walk_forward.py
  strategy/
    sma_cross.py
    volume_trend_factor.py
tests/
Makefile
```
