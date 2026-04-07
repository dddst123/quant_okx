from __future__ import annotations

import csv
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev

from okx_quant.client import OkxRestClient
from okx_quant.config import Settings
from okx_quant.market_state import MarketStateEngine
from okx_quant.models import Candle, SpotTicker
from okx_quant.portfolio_risk import PortfolioRiskEngine, PortfolioRiskState
from okx_quant.strategy import FactorCandidate, VolumeTrendFactorStrategy
from okx_quant.timeframe import bar_timedelta, bars_per_year
from okx_quant.universe import discover_factor_universe


@dataclass(frozen=True)
class BacktestTrade:
    ts: datetime
    inst_id: str
    side: str
    size: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    reason: str


@dataclass(frozen=True)
class EquityPoint:
    ts: datetime
    equity: Decimal
    cash: Decimal
    drawdown: Decimal
    benchmark_equity: Decimal | None


@dataclass(frozen=True)
class BacktestReport:
    years: int
    start: str
    end: str
    initial_capital: str
    ending_equity: str
    total_return: float
    cagr: float
    annualized_volatility: float
    sharpe_ratio: float
    max_drawdown: float
    benchmark_return: float | None
    total_trades: int
    turnover_ratio: float
    stop_loss_events: int
    drawdown_halts: int
    universe: list[str]
    picks_last: list[str]
    report_path: str
    equity_curve_path: str


class FactorBacktester:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OkxRestClient(settings, timeout=20)
        self.strategy = VolumeTrendFactorStrategy(settings)
        self.market_state_engine = MarketStateEngine(settings)
        self.risk_engine = PortfolioRiskEngine(settings)
        self.logger = logging.getLogger("okx_quant.backtest")

    def _ticker_map(self) -> dict[str, SpotTicker]:
        return {ticker.inst_id: ticker for ticker in self.client.get_spot_tickers()}

    def _discover_universe(self, ticker_map: dict[str, SpotTicker]) -> list[str]:
        if self.settings.factor_universe:
            return list(self.settings.factor_universe)
        allowed = {
            item["instId"]
            for item in self.client.list_spot_instruments(self.settings.factor_quote_currency)
            if item.get("state") == "live"
        }
        return discover_factor_universe(
            self.settings,
            ticker_map,
            allowed,
            lambda inst_id, limit: self.client.get_candles(inst_id, self.settings.factor_bar, limit),
        )

    def _fetch_history(self, inst_ids: list[str], years: int) -> dict[str, list[Candle]]:
        bars_needed = self.settings.factor_min_history + math.ceil(years * bars_per_year(self.settings.factor_bar) * (370.0 / 365.0)) + 10
        history: dict[str, list[Candle]] = {}
        for index, inst_id in enumerate(inst_ids):
            history[inst_id] = self.client.get_history_candles_paginated(inst_id, self.settings.factor_bar, bars_needed)
            if index != len(inst_ids) - 1:
                time.sleep(0.12)
        return history

    def _historically_liquid(self, inst_id: str, candles: list[Candle]) -> bool:
        if inst_id == self.settings.factor_benchmark_inst_id:
            return True
        if len(candles) < self.settings.factor_volume_lookback:
            return False
        avg_quote_volume = sum(
            candle.quote_volume or candle.volume * candle.close for candle in candles[-self.settings.factor_volume_lookback :]
        ) / Decimal(self.settings.factor_volume_lookback)
        return avg_quote_volume >= self.settings.factor_min_24h_quote_volume

    def _index_candles(self, history: dict[str, list[Candle]]) -> dict[str, dict[datetime, int]]:
        return {inst_id: {candle.ts: index for index, candle in enumerate(candles)} for inst_id, candles in history.items()}

    def _market_data_until(
        self,
        history: dict[str, list[Candle]],
        indexed: dict[str, dict[datetime, int]],
        ts: datetime,
    ) -> dict[str, list[Candle]]:
        market_data: dict[str, list[Candle]] = {}
        for inst_id, candles in history.items():
            index = indexed[inst_id].get(ts)
            if index is None:
                continue
            subset = candles[: index + 1]
            if len(subset) < self.settings.factor_min_history:
                continue
            if not self._historically_liquid(inst_id, subset):
                continue
            market_data[inst_id] = subset
        return market_data

    def _portfolio_equity(self, cash: Decimal, holdings: dict[str, Decimal], prices: dict[str, Decimal]) -> Decimal:
        return cash + sum(size * prices.get(inst_id, Decimal("0")) for inst_id, size in holdings.items())

    def _execute_sell(
        self,
        trades: list[BacktestTrade],
        holdings: dict[str, Decimal],
        cash: Decimal,
        ts: datetime,
        inst_id: str,
        size: Decimal,
        exec_price: Decimal,
        reason: str,
    ) -> Decimal:
        if size <= 0 or exec_price <= 0:
            return cash
        notional = size * exec_price
        fee = notional * (self.settings.factor_backtest_fee_rate + self.settings.factor_backtest_slippage_rate)
        holdings[inst_id] = max(Decimal("0"), holdings.get(inst_id, Decimal("0")) - size)
        if holdings[inst_id] == 0:
            holdings.pop(inst_id, None)
        trades.append(BacktestTrade(ts, inst_id, "sell", size, exec_price, notional, fee, reason))
        return cash + notional - fee

    def _execute_buy(
        self,
        trades: list[BacktestTrade],
        holdings: dict[str, Decimal],
        cash: Decimal,
        ts: datetime,
        inst_id: str,
        size: Decimal,
        exec_price: Decimal,
        reason: str,
    ) -> Decimal:
        if size <= 0 or exec_price <= 0:
            return cash
        notional = size * exec_price
        fee = notional * (self.settings.factor_backtest_fee_rate + self.settings.factor_backtest_slippage_rate)
        total_cost = notional + fee
        if total_cost > cash:
            return cash
        holdings[inst_id] = holdings.get(inst_id, Decimal("0")) + size
        trades.append(BacktestTrade(ts, inst_id, "buy", size, exec_price, notional, fee, reason))
        return cash - total_cost

    def _rebalance_due(self, last_rebalance_ts: datetime | None, current_ts: datetime, current_index: int) -> bool:
        if current_index % self.settings.factor_backtest_rebalance_every_bars != 0:
            return False
        if last_rebalance_ts is None:
            return True
        mode = self.settings.factor_rebalance_mode
        if mode == "interval":
            return (current_ts - last_rebalance_ts).total_seconds() >= self.settings.factor_rebalance_interval_sec
        if mode == "daily":
            return last_rebalance_ts.date() != current_ts.date()
        if mode == "weekly":
            return current_ts.weekday() == self.settings.factor_rebalance_weekday and current_ts.isocalendar()[:2] != last_rebalance_ts.isocalendar()[:2]
        return (current_ts.year, current_ts.month) != (last_rebalance_ts.year, last_rebalance_ts.month)

    def _write_outputs(self, report: BacktestReport, equity_curve: list[EquityPoint], trades: list[BacktestTrade]) -> None:
        output_dir = Path(self.settings.factor_backtest_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        report_payload = asdict(report)
        report_payload["trades"] = [
            {
                "ts": trade.ts.isoformat(),
                "inst_id": trade.inst_id,
                "side": trade.side,
                "size": str(trade.size),
                "price": str(trade.price),
                "notional": str(trade.notional),
                "fee": str(trade.fee),
                "reason": trade.reason,
            }
            for trade in trades
        ]
        Path(report.report_path).write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")

        with Path(report.equity_curve_path).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ts", "equity", "cash", "drawdown", "benchmark_equity"])
            for point in equity_curve:
                writer.writerow(
                    [
                        point.ts.isoformat(),
                        str(point.equity),
                        str(point.cash),
                        str(point.drawdown),
                        "" if point.benchmark_equity is None else str(point.benchmark_equity),
                    ]
                )

    def _simulate_window(
        self,
        history: dict[str, list[Candle]],
        universe: list[str],
        start_ts: datetime,
        end_ts: datetime,
        years: int,
        label: str,
    ) -> tuple[BacktestReport, list[EquityPoint], list[BacktestTrade]]:
        indexed = self._index_candles(history)
        all_ts = sorted({candle.ts for candles in history.values() for candle in candles if candle.confirmed})
        if not all_ts:
            raise RuntimeError("No historical candles returned for backtest")
        calendar = [
            ts
            for ts in all_ts
            if start_ts - bar_timedelta(self.settings.factor_bar, self.settings.factor_min_history + 5) <= ts <= end_ts
        ]
        if len(calendar) < self.settings.factor_min_history + 2:
            raise RuntimeError("Not enough historical bars for backtest")

        benchmark_history = history.get(self.settings.factor_benchmark_inst_id, [])
        benchmark_map = {candle.ts: candle.close for candle in benchmark_history}
        benchmark_start = next((benchmark_map[ts] for ts in calendar if ts in benchmark_map and ts >= start_ts), None)

        trades: list[BacktestTrade] = []
        equity_curve: list[EquityPoint] = []
        holdings: dict[str, Decimal] = {}
        cash = self.settings.factor_backtest_initial_capital
        risk_state = PortfolioRiskState(equity_peak=cash)
        stop_loss_events = 0
        drawdown_halts = 0
        last_picks: list[FactorCandidate] = []
        turnover_notional = Decimal("0")
        last_rebalance_ts: datetime | None = None

        for current_index in range(1, len(calendar)):
            prev_ts = calendar[current_index - 1]
            current_ts = calendar[current_index]
            if current_ts < start_ts:
                continue

            prev_market_data = self._market_data_until(history, indexed, prev_ts)
            prev_close_prices = {
                inst_id: candles[indexed[inst_id][prev_ts]].close
                for inst_id, candles in history.items()
                if prev_ts in indexed[inst_id]
            }
            current_open_prices = {
                inst_id: candles[indexed[inst_id][current_ts]].open
                for inst_id, candles in history.items()
                if current_ts in indexed[inst_id]
            }
            current_close_prices = {
                inst_id: candles[indexed[inst_id][current_ts]].close
                for inst_id, candles in history.items()
                if current_ts in indexed[inst_id]
            }
            if not prev_market_data:
                continue

            benchmark_data = prev_market_data.get(self.settings.factor_benchmark_inst_id)
            if benchmark_data is None and self.settings.factor_benchmark_inst_id in history and prev_ts in indexed[self.settings.factor_benchmark_inst_id]:
                bench_index = indexed[self.settings.factor_benchmark_inst_id][prev_ts]
                benchmark_data = history[self.settings.factor_benchmark_inst_id][: bench_index + 1]
            resume_ready = False
            if benchmark_data:
                resume_ready = self.strategy.resume_ready(prev_market_data, benchmark_data)

            risk_state = self.risk_engine.sync_positions(risk_state, holdings, prev_close_prices)
            equity_before = self._portfolio_equity(cash, holdings, prev_close_prices)
            risk_state, drawdown_before, drawdown_triggered = self.risk_engine.apply_drawdown(
                risk_state,
                equity_before,
                ts=current_ts,
            )
            resumed = False
            if drawdown_triggered and self.settings.factor_liquidate_on_halt:
                drawdown_halts += 1
            elif risk_state.trading_halted:
                risk_state, resumed = self.risk_engine.maybe_resume(
                    risk_state,
                    current_ts,
                    equity_before,
                    benchmark_trend_on=resume_ready,
                )

            stop_map = {decision.inst_id: decision for decision in self.risk_engine.stop_decisions(risk_state, holdings, prev_close_prices)}
            stop_loss_events += len(stop_map)
            rebalance_due = self._rebalance_due(last_rebalance_ts, current_ts, current_index)
            market_state = None
            if benchmark_data and self.settings.factor_market_state_enabled:
                market_state = self.market_state_engine.snapshot(prev_market_data, benchmark_data)

            if rebalance_due and not risk_state.trading_halted and benchmark_data:
                last_picks = self.strategy.evaluate(
                    prev_market_data,
                    benchmark_data=benchmark_data,
                    current_positions=set(holdings),
                    market_state=market_state,
                )
            elif rebalance_due:
                last_picks = []

            risk_budget_multiplier = Decimal("0") if risk_state.trading_halted else self.risk_engine.exposure_multiplier(
                Decimal("0") if resumed else drawdown_before
            )
            deployable_capital = min(
                equity_before * self.settings.factor_capital_fraction,
                max(equity_before - self.settings.min_cash_reserve_quote, Decimal("0")),
            )
            deployable_capital *= risk_budget_multiplier
            target_notional = {
                pick.inst_id: deployable_capital * Decimal(str(pick.weight))
                for pick in last_picks
                if pick.weight > 0 and rebalance_due and not risk_state.trading_halted
            }
            current_notional = {
                inst_id: holdings.get(inst_id, Decimal("0")) * prev_close_prices.get(inst_id, Decimal("0"))
                for inst_id in set(target_notional) | set(holdings)
            }
            raw_gaps = {
                inst_id: target_notional.get(inst_id, Decimal("0")) - current_notional.get(inst_id, Decimal("0"))
                for inst_id in set(target_notional) | set(holdings)
                if inst_id not in stop_map
            }
            gross_turnover = sum(abs(gap) for gap in raw_gaps.values())
            turnover_limit = equity_before * self.settings.factor_max_turnover_per_rebalance
            turnover_scale = Decimal("1")
            if rebalance_due and turnover_limit > 0 and gross_turnover > turnover_limit:
                turnover_scale = turnover_limit / gross_turnover

            # Forced risk-off sells first.
            for inst_id, current_size in list(holdings.items()):
                exec_price = current_open_prices.get(inst_id)
                if exec_price is None or current_size <= 0:
                    continue
                if risk_state.trading_halted and self.settings.factor_liquidate_on_halt:
                    cash = self._execute_sell(
                        trades,
                        holdings,
                        cash,
                        current_ts,
                        inst_id,
                        current_size,
                        exec_price,
                        risk_state.halt_reason or "drawdown halt",
                    )
                    turnover_notional += current_size * exec_price
                    continue
                stop_decision = stop_map.get(inst_id)
                if stop_decision is not None:
                    cash = self._execute_sell(
                        trades,
                        holdings,
                        cash,
                        current_ts,
                        inst_id,
                        current_size,
                        exec_price,
                        stop_decision.reason,
                    )
                    turnover_notional += current_size * exec_price

            if rebalance_due and not risk_state.trading_halted:
                for inst_id in sorted(set(target_notional) | set(holdings)):
                    if inst_id in stop_map:
                        continue
                    exec_price = current_open_prices.get(inst_id)
                    if exec_price is None or exec_price <= 0:
                        continue
                    scaled_gap = raw_gaps.get(inst_id, Decimal("0")) * turnover_scale
                    if abs(scaled_gap) < self.settings.factor_min_order_quote:
                        continue
                    current_size = holdings.get(inst_id, Decimal("0"))
                    if scaled_gap < 0:
                        sell_size = min(current_size, abs(scaled_gap) / exec_price)
                        cash = self._execute_sell(
                            trades,
                            holdings,
                            cash,
                            current_ts,
                            inst_id,
                            sell_size,
                            exec_price,
                            "rebalance down to target",
                        )
                        turnover_notional += sell_size * exec_price

                candidate_by_id = {pick.inst_id: pick for pick in last_picks}
                for inst_id in sorted(target_notional):
                    if inst_id in stop_map:
                        continue
                    exec_price = current_open_prices.get(inst_id)
                    if exec_price is None or exec_price <= 0:
                        continue
                    if market_state is not None and not market_state.entries_allowed:
                        continue
                    scaled_gap = raw_gaps.get(inst_id, Decimal("0")) * turnover_scale
                    if scaled_gap < self.settings.factor_min_order_quote:
                        continue
                    fee_buffer = Decimal("1") + self.settings.factor_backtest_fee_rate + self.settings.factor_backtest_slippage_rate
                    spend = min(scaled_gap, cash / fee_buffer)
                    if spend < self.settings.factor_min_order_quote:
                        continue
                    size = spend / exec_price
                    candidate = candidate_by_id[inst_id]
                    cash_before = cash
                    cash = self._execute_buy(
                        trades,
                        holdings,
                        cash,
                        current_ts,
                        inst_id,
                        size,
                        exec_price,
                        f"target weight {candidate.weight:.2%} score={candidate.score:.4f}",
                    )
                    turnover_notional += max(cash_before - cash, Decimal("0"))
                last_rebalance_ts = current_ts

            risk_state = self.risk_engine.sync_positions(risk_state, holdings, current_close_prices)
            equity_after = self._portfolio_equity(cash, holdings, current_close_prices)
            peak_after = max(risk_state.equity_peak, equity_after)
            drawdown_after = Decimal("0") if peak_after <= 0 else (peak_after - equity_after) / peak_after
            risk_state = PortfolioRiskState(
                equity_peak=peak_after,
                trading_halted=risk_state.trading_halted,
                halt_reason=risk_state.halt_reason,
                halted_at=risk_state.halted_at,
                last_rebalance_at=current_ts.isoformat() if last_rebalance_ts == current_ts else risk_state.last_rebalance_at,
                consecutive_errors=risk_state.consecutive_errors,
                positions=risk_state.positions,
            )
            benchmark_equity = None
            if benchmark_start is not None and current_ts in benchmark_map:
                benchmark_equity = self.settings.factor_backtest_initial_capital * (benchmark_map[current_ts] / benchmark_start)
            equity_curve.append(EquityPoint(current_ts, equity_after, cash, drawdown_after, benchmark_equity))

        if not equity_curve:
            raise RuntimeError("Backtest produced no equity points")

        returns = []
        for index in range(1, len(equity_curve)):
            previous = float(equity_curve[index - 1].equity)
            current = float(equity_curve[index].equity)
            if previous > 0:
                returns.append(current / previous - 1.0)
        returns_stdev = pstdev(returns) if len(returns) > 1 else 0.0
        annualization = math.sqrt(bars_per_year(self.settings.factor_bar))
        annualized_volatility = returns_stdev * annualization if returns_stdev > 0 else 0.0
        sharpe_ratio = (mean(returns) / returns_stdev * annualization) if returns_stdev > 0 else 0.0
        ending_equity = equity_curve[-1].equity
        total_return = float(ending_equity / self.settings.factor_backtest_initial_capital - Decimal("1"))
        elapsed_days = max((equity_curve[-1].ts - equity_curve[0].ts).days, 1)
        cagr = float((ending_equity / self.settings.factor_backtest_initial_capital) ** (Decimal("365") / Decimal(elapsed_days)) - Decimal("1"))
        max_drawdown = float(max(point.drawdown for point in equity_curve))
        benchmark_return = None
        if benchmark_start is not None and equity_curve[-1].ts in benchmark_map:
            benchmark_return = float(benchmark_map[equity_curve[-1].ts] / benchmark_start - Decimal("1"))

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        output_dir = Path(self.settings.factor_backtest_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        report_stem = f"factor_backtest_{label}_{timestamp}"
        report = BacktestReport(
            years=years,
            start=equity_curve[0].ts.date().isoformat(),
            end=equity_curve[-1].ts.date().isoformat(),
            initial_capital=str(self.settings.factor_backtest_initial_capital),
            ending_equity=str(ending_equity),
            total_return=total_return,
            cagr=cagr,
            annualized_volatility=annualized_volatility,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_drawdown,
            benchmark_return=benchmark_return,
            total_trades=len(trades),
            turnover_ratio=float(turnover_notional / self.settings.factor_backtest_initial_capital),
            stop_loss_events=stop_loss_events,
            drawdown_halts=drawdown_halts,
            universe=universe,
            picks_last=[pick.inst_id for pick in last_picks],
            report_path=str(output_dir / f"{report_stem}.json"),
            equity_curve_path=str(output_dir / f"{report_stem}_equity.csv"),
        )
        return report, equity_curve, trades

    def _simulate(self, history: dict[str, list[Candle]], universe: list[str], years: int) -> tuple[BacktestReport, list[EquityPoint], list[BacktestTrade]]:
        all_ts = sorted({candle.ts for candles in history.values() for candle in candles if candle.confirmed})
        if not all_ts:
            raise RuntimeError("No historical candles returned for backtest")
        end_ts = all_ts[-1]
        start_cutoff = end_ts - timedelta(days=365 * years)
        return self._simulate_window(history, universe, start_cutoff, end_ts, years, f"{years}y")

    def simulate_range(
        self,
        history: dict[str, list[Candle]],
        universe: list[str],
        start_ts: datetime,
        end_ts: datetime,
        *,
        label: str = "range",
    ) -> tuple[BacktestReport, list[EquityPoint], list[BacktestTrade]]:
        return self._simulate_window(history, universe, start_ts, end_ts, 0, label)

    def load_history_for_years(self, years_list: list[int]) -> tuple[list[str], dict[str, list[Candle]]]:
        max_years = max(years_list)
        ticker_map = self._ticker_map()
        universe = self._discover_universe(ticker_map)
        universe_for_fetch = list(dict.fromkeys(universe + [self.settings.factor_benchmark_inst_id]))
        history = self._fetch_history(universe_for_fetch, max_years)
        return universe, history

    def run(self, years: int) -> BacktestReport:
        universe, history = self.load_history_for_years([years])
        report, equity_curve, trades = self._simulate(history, universe, years)
        self._write_outputs(report, equity_curve, trades)
        return report

    def run_many(self, years_list: list[int]) -> list[BacktestReport]:
        universe, history = self.load_history_for_years(years_list)
        reports: list[BacktestReport] = []
        for years in years_list:
            self.logger.info("Running %sy backtest", years)
            report, equity_curve, trades = self._simulate(history, universe, years)
            self._write_outputs(report, equity_curve, trades)
            reports.append(report)
        return reports
