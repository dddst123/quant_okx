PYTHON ?= python3
SIM_ENV ?= .env.simulated
LIVE_ENV ?= .env.live

.PHONY: help sim-market-state sim-once sim-run sim-guard sim-guard-once sim-dashboard live-dry-run live-once live-dashboard wf-quick backtest check-sim-env check-live-env

help:
	@printf '%s\n' \
		'make sim-market-state # inspect the simulated market-state snapshot' \
		'make sim-once       # run one simulated portfolio cycle' \
		'make sim-run        # run the simulated continuous portfolio loop' \
		'make sim-guard      # run the simulated guardian daemon' \
		'make sim-guard-once # run one simulated guardian tick' \
		'make sim-dashboard  # serve the local simulated dashboard on 127.0.0.1:8787' \
		'make live-dry-run   # run one live-env cycle with OKX_DRY_RUN=true' \
		'make live-once      # run one live-env cycle (uses current .env.live values)' \
		'make live-dashboard # serve the local live dashboard on 127.0.0.1:8787' \
		'make wf-quick       # quick walk-forward smoke test' \
		'make backtest       # 1/2/3 year backtest'

check-sim-env:
	@for key in OKX_API_KEY OKX_API_SECRET OKX_API_PASSPHRASE; do \
		if ! grep -Eq "^$${key}=.+" "$(SIM_ENV)"; then \
			echo "Missing $$key in $(SIM_ENV). Fill the key first."; \
			exit 1; \
		fi; \
	done

check-live-env:
	@for key in OKX_API_KEY OKX_API_SECRET OKX_API_PASSPHRASE; do \
		if ! grep -Eq "^$${key}=.+" "$(LIVE_ENV)"; then \
			echo "Missing $$key in $(LIVE_ENV). Fill the key first."; \
			exit 1; \
		fi; \
	done

sim-once: check-sim-env
	OKX_ENV_FILE=$(SIM_ENV) $(PYTHON) -m okx_quant.main factors-once

sim-market-state:
	OKX_ENV_FILE=$(SIM_ENV) $(PYTHON) -m okx_quant.main factors-market-state

sim-run: check-sim-env
	OKX_ENV_FILE=$(SIM_ENV) $(PYTHON) -m okx_quant.main factors-run

sim-guard: check-sim-env
	OKX_ENV_FILE=$(SIM_ENV) $(PYTHON) -m okx_quant.main factors-guard

sim-dashboard:
	OKX_ENV_FILE=$(SIM_ENV) $(PYTHON) -m okx_quant.main factors-dashboard

sim-guard-once: check-sim-env
	OKX_ENV_FILE=$(SIM_ENV) $(PYTHON) -m okx_quant.main factors-guard --once

live-dry-run: check-live-env
	OKX_ENV_FILE=$(LIVE_ENV) OKX_DRY_RUN=true $(PYTHON) -m okx_quant.main factors-once

live-once: check-live-env
	OKX_ENV_FILE=$(LIVE_ENV) $(PYTHON) -m okx_quant.main factors-once

live-dashboard:
	OKX_ENV_FILE=$(LIVE_ENV) $(PYTHON) -m okx_quant.main factors-dashboard

wf-quick:
	$(PYTHON) -m okx_quant.main factors-walk-forward --lookback-years 2 --train-days 240 --test-days 60 --step-days 120 --search-profile quick

backtest:
	$(PYTHON) -m okx_quant.main factors-backtest --years 1 2 3
