# OKX Quant MVP

默认说明文档为英文版： [README.md](README.md)

这是一个面向 OKX 现货自动化交易的实用起点，仓库里包含单品种样例、多资产因子组合、历史回测、walk-forward、market-state 观察层，以及偏实盘化的风控与守护进程能力。

## 包含的内容

- `SMA` 单品种样例机器人，用来理解 OKX API、信号和基础执行流程。
- `VolumeTrendFactorStrategy` 多资产现货轮动策略。
- 历史回测和滚动 walk-forward 验证。
- market-state 观察与降风险挂钩能力。
- 止损、最大回撤熔断、状态持久化、日志轮转、异常重试、告警与长期运行 guardian。
- 用于模拟盘、实盘 dry-run 和研究验证的一键 `make` 命令。

## 公开仓库与私有研究的边界

现在这个仓库默认只公开代码、文档和安全模板。

建议始终保留在本地、不要随意上传的内容：

- API Key / Secret / Passphrase。
- 调优后的因子参数与 market-state 阈值。
- 不想公开的本地回测、walk-forward 报告。
- 实盘或模拟盘运行状态。

下面这些路径已经被 Git 忽略，适合放私有内容：

- `.env`
- `.env.simulated`
- `.env.live`
- `reports/`
- `logs/`
- `state/`

仓库里被跟踪的 `*.example` 环境文件现在只保留安全的运行模板，不再放核心调优参数。程序本身会先使用 `/Users/mac/project/quant_okx/okx_quant/config.py` 里的默认值，你可以再在本地忽略文件中做私有覆盖。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
cp .env.simulated.example .env.simulated
cp .env.live.example .env.live
```

然后只在本地忽略文件里填写 key：

- `/Users/mac/project/quant_okx/.env.simulated`：OKX 模拟盘。
- `/Users/mac/project/quant_okx/.env.live`：OKX 实盘。

## 环境变量加载顺序

配置按以下顺序加载：

1. `/Users/mac/project/quant_okx/.env`
2. 如果启动时传了 `OKX_ENV_FILE=/path/to/private.env`
3. `OKX_ENV_PROFILE=simulated` -> `/Users/mac/project/quant_okx/.env.simulated`
4. `OKX_ENV_PROFILE=live` -> `/Users/mac/project/quant_okx/.env.live`

推荐做法：

- `/Users/mac/project/quant_okx/.env`：放共享但不敏感的设置。
- `/Users/mac/project/quant_okx/.env.simulated`：放模拟盘 key 和私有模拟盘参数。
- `/Users/mac/project/quant_okx/.env.live`：放实盘 key 和更保守的私有实盘参数。

如果你后面想维护多套本地参数文件，也可以新建额外的忽略 env，并通过 `OKX_ENV_FILE=/absolute/path/to/private.env` 启动。

## 一键命令

在 `/Users/mac/project/quant_okx` 下执行：

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

说明：

- `make sim-market-state`：查看当前模拟盘 market-state 快照。
- `make sim-once`：执行一次模拟盘组合决策。
- `make sim-run`：持续运行模拟盘组合。
- `make sim-guard-once`：执行一次 guardian tick。
- `make sim-guard`：启动长期运行的 guardian 守护进程。
- `make sim-dashboard`：在 `http://127.0.0.1:8787` 启动本地网页看板。
- `make live-dry-run`：读取 `/Users/mac/project/quant_okx/.env.live`，但强制 `OKX_DRY_RUN=true`。
- `make live-once`：按当前本地实盘配置执行一次。
- `make live-dashboard`：按实盘 profile 的日志/报告路径启动本地网页看板。
- `make wf-quick`：执行较轻量的 walk-forward 烟雾测试。
- `make backtest`：执行 1/2/3 年回测批处理。

## 常用 CLI

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

## 本地网页看板

这个 dashboard 只监听本机地址，会直接读取 guardian 的本地事件日志并展示：

- guardian tick 形成的实时权益 / NAV / 回撤曲线
- 最新持仓、候选 picks、计划订单和 market-state 原因
- `/Users/mac/project/quant_okx/reports/backtests` 里最新一次回测摘要

推荐用法：

1. 一个终端运行 guardian：`make sim-guard`
2. 另一个终端运行 dashboard：`make sim-dashboard`
3. 浏览器打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)

如果页面还没有实时数据，通常表示 guardian 还没往本地日志写入新的 tick。

## 建议的验证路径

不要一上来就直接接真实资金，建议按这个顺序推进：

1. 先对当前本地私有参数做回测。
2. 再做 walk-forward 看样本外表现。
3. 接 OKX 模拟盘做 forward test。
4. 即便切到实盘配置，也先保持 `OKX_DRY_RUN=true`，观察日志、告警和 guardian 是否稳定。
5. 最后再考虑小资金实盘。

## 安全建议

- 交易 API 不要开提现权限。
- 没完成模拟盘验证前，实盘配置保持 dry-run。
- 把调优后的核心参数视为私有研究资产，不要当作公开默认值。
- 开启真实下单前，先检查日志和 guardian 输出是否符合预期。

## 目录结构

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
