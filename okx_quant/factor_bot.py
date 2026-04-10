from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, TypeVar

import requests  # type: ignore[import-untyped]

from okx_quant.alerts import AlertManager
from okx_quant.client import OkxApiError, OkxRestClient
from okx_quant.config import Settings
from okx_quant.market_state import MarketStateEngine, MarketStateSnapshot
from okx_quant.models import InstrumentRules, SpotTicker
from okx_quant.portfolio_risk import PortfolioRiskEngine, PortfolioRiskState, PortfolioStateStore
from okx_quant.strategy import FactorCandidate, VolumeTrendFactorStrategy
from okx_quant.timeframe import bar_seconds
from okx_quant.universe import discover_factor_universe

T = TypeVar("T")


@dataclass(frozen=True)
class PlannedOrder:
    inst_id: str
    side: str
    size: Decimal
    target_currency: str
    est_quote_value: Decimal
    reason: str


@dataclass(frozen=True)
class FactorRunSnapshot:
    ts: datetime
    total_equity_quote: Decimal
    available_quote: Decimal
    drawdown: Decimal
    trading_halted: bool
    halt_reason: str
    holdings: dict[str, Decimal]
    holdings_quote: dict[str, Decimal]
    picks: list[FactorCandidate]
    planned_orders: list[PlannedOrder]
    executed_orders: list[dict[str, Any]]
    market_state: MarketStateSnapshot | None = None


class FactorPortfolioBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OkxRestClient(settings)
        self.strategy = VolumeTrendFactorStrategy(settings)
        self.market_state_engine = MarketStateEngine(settings)
        self.logger = logging.getLogger("okx_quant.factor_bot")
        self.alerts = AlertManager(settings)
        self.risk_engine = PortfolioRiskEngine(settings)
        self.state_store = PortfolioStateStore(settings.factor_state_path)
        self._rules_cache: dict[str, InstrumentRules] = {}

    def _round_down(self, value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        units = (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN)
        return units * step

    def _with_retry(self, action: Callable[[], T], label: str) -> T:
        for attempt in range(self.settings.http_max_retries):
            try:
                return action()
            except OkxApiError:
                # Business/logic errors (e.g., insufficient balance, market suspended) are
                # not transient — do not retry, propagate immediately.
                raise
            except requests.RequestException:
                if attempt == self.settings.http_max_retries - 1:
                    raise
                self.logger.warning("Retrying %s after network failure (%s/%s)", label, attempt + 1, self.settings.http_max_retries)
                time.sleep(self.settings.http_retry_backoff_sec * (2**attempt))
            except Exception:
                # Unexpected errors (TypeError, ValueError, etc.) — also retry once.
                if attempt == self.settings.http_max_retries - 1:
                    raise
                self.logger.warning("Retrying %s after unexpected error (%s/%s)", label, attempt + 1, self.settings.http_max_retries)
                time.sleep(self.settings.http_retry_backoff_sec * (2**attempt))
        raise RuntimeError("Unreachable retry loop")

    def _ticker_map(self) -> dict[str, SpotTicker]:
        return {ticker.inst_id: ticker for ticker in self._with_retry(self.client.get_spot_tickers, "spot tickers")}

    def _discover_universe(self, ticker_map: dict[str, SpotTicker]) -> list[str]:
        if self.settings.factor_universe:
            return list(self.settings.factor_universe)
        allowed = {
            item["instId"]
            for item in self._with_retry(
                lambda: self.client.list_spot_instruments(self.settings.factor_quote_currency),
                "spot instruments",
            )
            if item.get("state") == "live"
        }
        return discover_factor_universe(
            self.settings,
            ticker_map,
            allowed,
            lambda inst_id, limit: self._fetch_recent_candles(inst_id, limit),
        )

    def _fetch_recent_candles(self, inst_id: str, limit: int | None = None) -> list:
        return self._with_retry(
            lambda: self.client.get_candles(
                inst_id,
                self.settings.factor_bar,
                min(limit or (self.settings.factor_min_history + 5), 300),
            ),
            f"candles for {inst_id}",
        )

    def _filled_price(
        self,
        order: PlannedOrder,
        fill_result: dict[str, Any],
        ticker_map: dict[str, SpotTicker],
    ) -> Decimal:
        for key in ("avgPx", "fillPx", "px"):
            raw_value = fill_result.get(key)
            if raw_value in (None, ""):
                continue
            try:
                price = Decimal(str(raw_value))
            except Exception:
                continue
            if price > 0:
                return price
        return ticker_map[order.inst_id].last

    def _fetch_market_data(self, inst_ids: list[str]) -> dict[str, list]:
        market_data = {}
        for index, inst_id in enumerate(inst_ids):
            market_data[inst_id] = self._fetch_recent_candles(inst_id)
            if index != len(inst_ids) - 1:
                time.sleep(0.12)
        return market_data

    def _ensure_benchmark_data(self, market_data: dict[str, list]) -> list:
        benchmark = self.settings.factor_benchmark_inst_id
        if benchmark in market_data:
            return market_data[benchmark]
        return self._fetch_recent_candles(benchmark)

    def rank_candidates(self) -> list[FactorCandidate]:
        ticker_map = self._ticker_map()
        universe = self._discover_universe(ticker_map)
        market_data = self._fetch_market_data(universe)
        benchmark_data = self._ensure_benchmark_data(market_data)
        market_state = self._build_market_state(market_data, benchmark_data)
        return self.strategy.evaluate(market_data, benchmark_data=benchmark_data, market_state=market_state)

    def _build_market_state(self, market_data: dict[str, list], benchmark_data: list) -> MarketStateSnapshot | None:
        if not self.settings.factor_market_state_enabled or not benchmark_data:
            return None
        public_state = self.market_state_engine.collect_public_state(self.client)
        return self.market_state_engine.snapshot(market_data, benchmark_data, public_state=public_state)

    def inspect_market_state(self) -> MarketStateSnapshot:
        ticker_map = self._ticker_map()
        universe = self._discover_universe(ticker_map)
        market_data = self._fetch_market_data(universe)
        benchmark_data = self._ensure_benchmark_data(market_data)
        market_state = self._build_market_state(market_data, benchmark_data)
        if market_state is None:
            raise RuntimeError("Market-state module is disabled")
        return market_state

    def _get_rules(self, inst_id: str) -> InstrumentRules:
        if inst_id not in self._rules_cache:
            self._rules_cache[inst_id] = self._with_retry(
                lambda: self.client.get_instrument_rules(inst_id),
                f"instrument rules for {inst_id}",
            )
        return self._rules_cache[inst_id]

    def _spot_holdings(self, balances: dict[str, Decimal], ticker_map: dict[str, SpotTicker]) -> dict[str, Decimal]:
        holdings: dict[str, Decimal] = {}
        suffix = f"-{self.settings.factor_quote_currency}"
        for currency, balance in balances.items():
            if currency == self.settings.factor_quote_currency or balance <= 0:
                continue
            inst_id = f"{currency}{suffix}"
            if inst_id in ticker_map:
                holdings[inst_id] = balance
        return holdings

    def _rebalance_due(self, state: PortfolioRiskState, now: datetime) -> bool:
        return self.settings.rebalance_due(state.last_rebalance_at, now)

    def _portfolio_equity(self, balances: dict[str, Decimal], ticker_map: dict[str, SpotTicker]) -> tuple[dict[str, Decimal], Decimal]:
        holdings = self._spot_holdings(balances, ticker_map)
        equity = balances.get(self.settings.factor_quote_currency, Decimal("0"))
        for inst_id, size in holdings.items():
            equity += size * ticker_map[inst_id].last
        return holdings, equity

    def _build_orders(
        self,
        picks: list[FactorCandidate],
        balances: dict[str, Decimal],
        ticker_map: dict[str, SpotTicker],
        trading_halted: bool = False,
        forced_sells: dict[str, str] | None = None,
        rebalance_enabled: bool = True,
        risk_budget_multiplier: Decimal = Decimal("1"),
        allow_new_entries: bool = True,
    ) -> tuple[Decimal, Decimal, list[PlannedOrder]]:
        forced_sells = forced_sells or {}
        quote_ccy = self.settings.factor_quote_currency
        available_quote = balances.get(quote_ccy, Decimal("0"))
        holdings, total_equity_quote = self._portfolio_equity(balances, ticker_map)

        deployable_capital = min(
            total_equity_quote * self.settings.factor_capital_fraction,
            max(total_equity_quote - self.settings.min_cash_reserve_quote, Decimal("0")),
        )
        deployable_capital *= risk_budget_multiplier
        target_notional = {
            pick.inst_id: deployable_capital * Decimal(str(pick.weight))
            for pick in picks
            if pick.weight > 0 and not trading_halted and rebalance_enabled
        }
        current_notional = {
            inst_id: base_balance * ticker_map[inst_id].last
            for inst_id, base_balance in holdings.items()
        }

        raw_gaps: dict[str, Decimal] = {}
        universe = sorted(set(target_notional) | set(current_notional))
        for inst_id in universe:
            if inst_id in forced_sells:
                continue
            raw_gaps[inst_id] = target_notional.get(inst_id, Decimal("0")) - current_notional.get(inst_id, Decimal("0"))
        gross_turnover = sum(abs(gap) for gap in raw_gaps.values())
        turnover_limit = total_equity_quote * self.settings.factor_max_turnover_per_rebalance
        turnover_scale = Decimal("1")
        if rebalance_enabled and turnover_limit > 0 and gross_turnover > turnover_limit:
            turnover_scale = turnover_limit / gross_turnover

        candidate_by_id = {pick.inst_id: pick for pick in picks}
        planned: list[PlannedOrder] = []
        for inst_id in sorted(set(universe) | set(forced_sells)):
            ticker = ticker_map.get(inst_id)
            if ticker is None:
                continue
            rules = self._get_rules(inst_id)
            current_size = holdings.get(inst_id, Decimal("0"))
            if inst_id in forced_sells and current_size > 0:
                size = self._round_down(current_size, rules.lot_size)
                est_quote_value = size * ticker.last
                if size >= rules.min_size and est_quote_value >= self.settings.factor_min_order_quote:
                    planned.append(
                        PlannedOrder(
                            inst_id=inst_id,
                            side="sell",
                            size=size,
                            target_currency="base_ccy",
                            est_quote_value=est_quote_value,
                            reason=forced_sells[inst_id],
                        )
                    )
                continue

            if not rebalance_enabled:
                continue

            gap = raw_gaps.get(inst_id, Decimal("0")) * turnover_scale
            if abs(gap) < self.settings.factor_min_order_quote:
                continue

            if gap > 0:
                if not allow_new_entries:
                    continue
                base_size = self._round_down(gap / ticker.last, rules.lot_size)
                est_quote_value = base_size * ticker.last
                if base_size < rules.min_size or est_quote_value < self.settings.factor_min_order_quote:
                    continue
                candidate = candidate_by_id.get(inst_id)
                if candidate is None:
                    continue
                reason = f"buy to target {inst_id} weight={candidate.weight:.2%} score={candidate.score:.4f}"
                planned.append(
                    PlannedOrder(
                        inst_id=inst_id,
                        side="buy",
                        size=base_size,
                        target_currency="base_ccy",
                        est_quote_value=est_quote_value,
                        reason=reason,
                    )
                )
                continue

            base_size = self._round_down(min(current_size, abs(gap) / ticker.last), rules.lot_size)
            est_quote_value = base_size * ticker.last
            if base_size < rules.min_size or est_quote_value < self.settings.factor_min_order_quote:
                continue
            planned.append(
                PlannedOrder(
                    inst_id=inst_id,
                    side="sell",
                    size=base_size,
                    target_currency="base_ccy",
                    est_quote_value=est_quote_value,
                    reason="sell because the asset is no longer in the target factor portfolio",
                )
            )

        planned.sort(key=lambda order: 0 if order.side == "sell" else 1)
        return total_equity_quote, available_quote, planned

    def run_once(self) -> FactorRunSnapshot:
        self.settings.require_private_api()
        now = datetime.now(UTC)
        state = self.state_store.load()
        ticker_map = self._ticker_map()
        price_map = {inst_id: ticker.last for inst_id, ticker in ticker_map.items()}
        balances = self._with_retry(self.client.get_all_balances, "account balances")
        holdings, total_equity_quote = self._portfolio_equity(balances, ticker_map)

        state = self.risk_engine.sync_positions(state, holdings, price_map)
        state, drawdown, drawdown_triggered = self.risk_engine.apply_drawdown(state, total_equity_quote)
        rebalance_due = self._rebalance_due(state, now)
        universe: list[str] = []
        market_data: dict[str, list] = {}
        benchmark_data: list = []
        market_state: MarketStateSnapshot | None = None
        if rebalance_due or state.trading_halted:
            universe = self._discover_universe(ticker_map)
            market_data = self._fetch_market_data(universe)
            benchmark_data = self._ensure_benchmark_data(market_data)
            market_state = self._build_market_state(market_data, benchmark_data)

        resumed = False
        if state.trading_halted and not drawdown_triggered and benchmark_data:
            state, resumed = self.risk_engine.maybe_resume(
                state,
                now,
                total_equity_quote,
                benchmark_trend_on=self.strategy.resume_ready(market_data, benchmark_data),
            )
            if resumed:
                self.alerts.send("Drawdown Resume", "Cooldown elapsed and benchmark trend recovered; trading resumed")
        stop_decisions = self.risk_engine.stop_decisions(state, holdings, price_map)
        forced_sells = {d.inst_id: d.reason for d in stop_decisions}
        # Reset high-water mark for trailing-stop positions so the next bar uses the
        # current price as baseline.
        stopped_ids: set[str] = set()
        for d in stop_decisions:
            stopped_ids |= d.stopped_position_ids
        if stopped_ids:
            state = self.risk_engine.reset_trailing_stops(state, stopped_ids, price_map)
        if drawdown_triggered and state.trading_halted:
            self.alerts.send("Drawdown Halt", state.halt_reason or "Max drawdown exceeded")
        for inst_id, reason in forced_sells.items():
            self.alerts.send("Stop Triggered", f"{inst_id}: {reason}")

        picks: list[FactorCandidate] = []
        if rebalance_due and not state.trading_halted:
            picks = self.strategy.evaluate(
                market_data,
                benchmark_data=benchmark_data,
                current_positions=set(holdings),
                market_state=market_state,
            )

        risk_budget_multiplier = Decimal("0") if state.trading_halted else self.risk_engine.exposure_multiplier(
            Decimal("0") if resumed else drawdown
        )
        _, _, planned_orders = self._build_orders(
            picks,
            balances,
            ticker_map,
            trading_halted=state.trading_halted,
            forced_sells=forced_sells,
            rebalance_enabled=rebalance_due,
            risk_budget_multiplier=risk_budget_multiplier,
            allow_new_entries=market_state.entries_allowed if market_state is not None else True,
        )

        executed_orders: list[dict[str, Any]] = []
        live_orders_submitted = False
        fill_tracking_failed = False
        for order in planned_orders:
            if self.settings.dry_run:
                self.logger.info(
                    "Dry-run %s %s est_notional=%s reason=%s",
                    order.side,
                    order.inst_id,
                    order.est_quote_value,
                    order.reason,
                )
                continue

            live_orders_submitted = True
            result = self.client.place_market_order(order.inst_id, order.side, order.size, order.target_currency)
            self.logger.info("Order sent %s %s: %s", order.side, order.inst_id, result)
            # Wait for the order to fill before proceeding to the next.
            try:
                fill_result = self.client.wait_for_fill(order.inst_id, result.get("clOrdId", ""), timeout_sec=15)
                executed_orders.append(fill_result)
                state = self.risk_engine.apply_fill(
                    state,
                    order.inst_id,
                    order.side,
                    order.size,
                    self._filled_price(order, fill_result, ticker_map),
                )
                self.logger.info("Order filled %s %s: %s", order.side, order.inst_id, fill_result)
            except Exception as exc:
                fill_tracking_failed = True
                self.logger.warning("Order fill wait failed for %s %s: %s", order.side, order.inst_id, exc)

        if rebalance_due and not fill_tracking_failed:
            state = self.risk_engine.mark_rebalance(state, now)
        elif rebalance_due and fill_tracking_failed:
            self.logger.warning("Rebalance confirmation incomplete; leaving rebalance pending for the next cycle")

        if live_orders_submitted:
            effective_balances = self._with_retry(self.client.get_all_balances, "account balances refresh")
        else:
            effective_balances = balances
        effective_holdings, effective_total_equity_quote = self._portfolio_equity(effective_balances, ticker_map)
        effective_available_quote = effective_balances.get(self.settings.factor_quote_currency, Decimal("0"))
        effective_holdings_quote = {
            inst_id: size * ticker_map[inst_id].last
            for inst_id, size in effective_holdings.items()
            if inst_id in ticker_map
        }
        state = self.risk_engine.sync_positions(state, effective_holdings, price_map)
        state = self.risk_engine.clear_errors(state)
        self.state_store.save(state)
        effective_drawdown = (
            Decimal("0")
            if state.equity_peak <= 0
            else max(Decimal("0"), (state.equity_peak - effective_total_equity_quote) / state.equity_peak)
        )

        return FactorRunSnapshot(
            ts=now,
            total_equity_quote=effective_total_equity_quote,
            available_quote=effective_available_quote,
            drawdown=effective_drawdown,
            trading_halted=state.trading_halted,
            halt_reason=state.halt_reason,
            holdings=effective_holdings,
            holdings_quote=effective_holdings_quote,
            picks=picks,
            planned_orders=planned_orders,
            executed_orders=executed_orders,
            market_state=market_state,
        )

    def serve_forever(self) -> None:
        base_sleep = self.settings.factor_rebalance_interval_sec
        if self.settings.factor_rebalance_mode != "interval":
            base_sleep = min(self.settings.factor_rebalance_interval_sec, bar_seconds(self.settings.factor_bar))
        sleep_seconds = base_sleep
        error_sleep = base_sleep
        while True:
            try:
                snapshot = self.run_once()
                self.logger.info(
                    "Factor tick %s equity=%s cash=%s drawdown=%s halted=%s picks=%s planned_orders=%s market_state=%s",
                    snapshot.ts.isoformat(),
                    snapshot.total_equity_quote,
                    snapshot.available_quote,
                    snapshot.drawdown,
                    snapshot.trading_halted,
                    [pick.inst_id for pick in snapshot.picks],
                    len(snapshot.planned_orders),
                    snapshot.market_state.reason if snapshot.market_state is not None else "n/a",
                )
                error_sleep = base_sleep
            except Exception as exc:
                state = self.risk_engine.record_error(self.state_store.load())
                self.state_store.save(state)
                self.logger.exception("Factor trading loop failed")
                if state.consecutive_errors == 1 or state.consecutive_errors % self.settings.alert_error_every_n == 0:
                    self.alerts.send("Factor Bot Error", f"consecutive_errors={state.consecutive_errors} error={exc}")
                # Exponential backoff capped at 30 minutes.
                error_sleep = min(error_sleep * 2, 1800)
                time.sleep(error_sleep)
                continue
            time.sleep(sleep_seconds)

    def reset_risk_state(self) -> None:
        self.state_store.reset()
        self.logger.info("Risk state reset at %s", self.settings.factor_state_path)
