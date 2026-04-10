from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from okx_quant.config import Settings


@dataclass
class PositionState:
    cost_basis: Decimal  # volume-weighted average entry price
    high_water_price: Decimal
    size: Decimal | None = None


@dataclass
class PortfolioRiskState:
    equity_peak: Decimal = Decimal("0")
    trading_halted: bool = False
    halt_reason: str = ""
    halted_at: str = ""
    last_rebalance_at: str = ""
    consecutive_errors: int = 0
    positions: dict[str, PositionState] = field(default_factory=dict)


@dataclass(frozen=True)
class StopDecision:
    inst_id: str
    reason: str
    stopped_position_ids: frozenset[str] = frozenset()


class PortfolioStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> PortfolioRiskState:
        if not self.path.exists():
            return PortfolioRiskState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        positions = {
            inst_id: PositionState(
                # Support both old (entry_price) and new (cost_basis) field names for backward compat.
                cost_basis=Decimal(
                    item.get("cost_basis") or item.get("entry_price", "0")
                ),
                high_water_price=Decimal(item.get("high_water_price", "0")),
                size=(
                    Decimal(str(item["size"]))
                    if item.get("size") not in (None, "")
                    else None
                ),
            )
            for inst_id, item in data.get("positions", {}).items()
        }
        return PortfolioRiskState(
            equity_peak=Decimal(data.get("equity_peak", "0")),
            trading_halted=bool(data.get("trading_halted", False)),
            halt_reason=data.get("halt_reason", ""),
            halted_at=data.get("halted_at", ""),
            last_rebalance_at=data.get("last_rebalance_at", ""),
            consecutive_errors=int(data.get("consecutive_errors", 0)),
            positions=positions,
        )

    def save(self, state: PortfolioRiskState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "equity_peak": str(state.equity_peak),
            "trading_halted": state.trading_halted,
            "halt_reason": state.halt_reason,
            "halted_at": state.halted_at,
            "last_rebalance_at": state.last_rebalance_at,
            "consecutive_errors": state.consecutive_errors,
            "positions": {
                inst_id: {
                    "cost_basis": str(item.cost_basis),
                    "high_water_price": str(item.high_water_price),
                    "size": None if item.size is None else str(item.size),
                }
                for inst_id, item in state.positions.items()
            },
        }
        # Atomic write: write to temp file, then atomically replace.
        tmp_path: str | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                dir=self.path.parent,
                prefix=".tmp_",
                suffix=".json",
                encoding="utf-8",
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
                json.dump(payload, tmp, indent=2, ensure_ascii=True)
            Path(tmp_path).replace(self.path)
        except BaseException:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
            raise

    def reset(self) -> PortfolioRiskState:
        state = PortfolioRiskState()
        self.save(state)
        return state


class PortfolioRiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("okx_quant.portfolio_risk")

    def exposure_multiplier(self, drawdown: Decimal) -> Decimal:
        multiplier = Decimal("1")
        for threshold, scaled in self.settings.factor_drawdown_scale_tiers:
            if drawdown >= threshold:
                multiplier = scaled
        return multiplier

    def sync_positions(
        self,
        state: PortfolioRiskState,
        holdings: dict[str, Decimal],
        price_map: dict[str, Decimal],
    ) -> PortfolioRiskState:
        positions: dict[str, PositionState] = {}
        for inst_id, size in holdings.items():
            if size <= 0 or inst_id not in price_map:
                continue
            price = price_map[inst_id]
            existing = state.positions.get(inst_id)
            if existing is None:
                # New position: record current price as both cost basis and high-water mark.
                positions[inst_id] = PositionState(cost_basis=price, high_water_price=price, size=size)
            else:
                cost_basis = existing.cost_basis
                if existing.size is not None and size > existing.size:
                    added_size = size - existing.size
                    weighted_cost = (existing.cost_basis * existing.size) + (price * added_size)
                    cost_basis = weighted_cost / size
                # Existing position: high-water mark advances. Cost basis stays
                # unchanged unless a larger position appears without confirmed fills,
                # in which case we approximate the added size at the current price.
                positions[inst_id] = PositionState(
                    cost_basis=cost_basis,
                    high_water_price=max(existing.high_water_price, price),
                    size=size,
                )
        return PortfolioRiskState(
            equity_peak=state.equity_peak,
            trading_halted=state.trading_halted,
            halt_reason=state.halt_reason,
            halted_at=state.halted_at,
            last_rebalance_at=state.last_rebalance_at,
            consecutive_errors=state.consecutive_errors,
            positions=positions,
        )

    def apply_fill(
        self,
        state: PortfolioRiskState,
        inst_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
    ) -> PortfolioRiskState:
        if size <= 0 or price <= 0:
            return state

        positions = dict(state.positions)
        existing = positions.get(inst_id)
        if side == "buy":
            if existing is None or existing.size is None or existing.size <= 0:
                positions[inst_id] = PositionState(cost_basis=price, high_water_price=price, size=size)
            else:
                new_size = existing.size + size
                weighted_cost = (existing.cost_basis * existing.size) + (price * size)
                positions[inst_id] = PositionState(
                    cost_basis=weighted_cost / new_size,
                    high_water_price=max(existing.high_water_price, price),
                    size=new_size,
                )
        elif existing is not None:
            if existing.size is not None:
                remaining_size = existing.size - size
                if remaining_size <= 0:
                    positions.pop(inst_id, None)
                else:
                    positions[inst_id] = PositionState(
                        cost_basis=existing.cost_basis,
                        high_water_price=max(existing.high_water_price, price),
                        size=remaining_size,
                    )

        return PortfolioRiskState(
            equity_peak=state.equity_peak,
            trading_halted=state.trading_halted,
            halt_reason=state.halt_reason,
            halted_at=state.halted_at,
            last_rebalance_at=state.last_rebalance_at,
            consecutive_errors=state.consecutive_errors,
            positions=positions,
        )

    def apply_drawdown(
        self,
        state: PortfolioRiskState,
        equity: Decimal,
        ts: datetime | None = None,
    ) -> tuple[PortfolioRiskState, Decimal, bool]:
        peak = max(state.equity_peak, equity)
        drawdown = Decimal("0") if peak <= 0 else (peak - equity) / peak
        should_halt = drawdown >= self.settings.factor_max_drawdown_pct
        halted_at = state.halted_at
        halt_reason = state.halt_reason
        trading_halted = state.trading_halted
        newly_triggered = should_halt and not state.trading_halted
        if newly_triggered:
            trading_halted = True
            halted_at = (ts or datetime.now(UTC)).isoformat()
            halt_reason = f"max drawdown {drawdown:.2%} >= {self.settings.factor_max_drawdown_pct:.2%}"
        return (
            PortfolioRiskState(
                equity_peak=peak,
                trading_halted=trading_halted,
                halt_reason=halt_reason,
                halted_at=halted_at,
                last_rebalance_at=state.last_rebalance_at,
                consecutive_errors=state.consecutive_errors,
                positions=state.positions,
            ),
            drawdown,
            newly_triggered,
        )

    def maybe_resume(
        self,
        state: PortfolioRiskState,
        ts: datetime,
        equity: Decimal,
        benchmark_trend_on: bool,
    ) -> tuple[PortfolioRiskState, bool]:
        if not state.trading_halted:
            return state, False
        if self.settings.factor_halt_cooldown_days <= 0 or not state.halted_at:
            return state, False
        if self.settings.factor_halt_resume_requires_benchmark_trend and not benchmark_trend_on:
            return state, False

        halted_at_dt = datetime.fromisoformat(state.halted_at)
        if ts < halted_at_dt + timedelta(days=self.settings.factor_halt_cooldown_days):
            return state, False

        resumed = PortfolioRiskState(
            equity_peak=equity,
            trading_halted=False,
            halt_reason="",
            halted_at="",
            last_rebalance_at=state.last_rebalance_at,
            consecutive_errors=state.consecutive_errors,
            positions=state.positions,
        )
        return resumed, True

    def stop_decisions(
        self,
        state: PortfolioRiskState,
        holdings: dict[str, Decimal],
        price_map: dict[str, Decimal],
    ) -> list[StopDecision]:
        """Return stop decisions. Trailing-stop triggered positions have their
        high_water_price reset to the current price so subsequent bars use the
        new baseline."""
        decisions: list[StopDecision] = []
        stopped_ids: set[str] = set()

        for inst_id, size in holdings.items():
            if size <= 0 or inst_id not in price_map:
                continue
            position = state.positions.get(inst_id)
            if position is None:
                continue
            price = price_map[inst_id]

            if self.settings.factor_stop_loss_pct > 0:
                stop_price = position.cost_basis * (Decimal("1") - self.settings.factor_stop_loss_pct)
                if price <= stop_price:
                    decisions.append(StopDecision(inst_id, f"hard stop hit at {price} <= {stop_price}"))
                    stopped_ids.add(inst_id)
                    continue

            if self.settings.factor_trailing_stop_pct > 0:
                trailing_price = position.high_water_price * (Decimal("1") - self.settings.factor_trailing_stop_pct)
                if price <= trailing_price:
                    decisions.append(StopDecision(inst_id, f"trailing stop hit at {price} <= {trailing_price}"))
                    stopped_ids.add(inst_id)

        if stopped_ids:
            self.logger.info("Stop decisions triggered for %s; resetting high_water_price", stopped_ids)

        # Return decisions with the set of stopped position IDs so the caller can
        # apply resets to the state.
        return [
            StopDecision(d.inst_id, d.reason, frozenset(stopped_ids)) if d.inst_id in stopped_ids else d
            for d in decisions
        ]

    def record_error(self, state: PortfolioRiskState) -> PortfolioRiskState:
        return PortfolioRiskState(
            equity_peak=state.equity_peak,
            trading_halted=state.trading_halted,
            halt_reason=state.halt_reason,
            halted_at=state.halted_at,
            last_rebalance_at=state.last_rebalance_at,
            consecutive_errors=state.consecutive_errors + 1,
            positions=state.positions,
        )

    def clear_errors(self, state: PortfolioRiskState) -> PortfolioRiskState:
        if state.consecutive_errors == 0:
            return state
        return PortfolioRiskState(
            equity_peak=state.equity_peak,
            trading_halted=state.trading_halted,
            halt_reason=state.halt_reason,
            halted_at=state.halted_at,
            last_rebalance_at=state.last_rebalance_at,
            consecutive_errors=0,
            positions=state.positions,
        )

    def mark_rebalance(self, state: PortfolioRiskState, ts: datetime) -> PortfolioRiskState:
        return PortfolioRiskState(
            equity_peak=state.equity_peak,
            trading_halted=state.trading_halted,
            halt_reason=state.halt_reason,
            halted_at=state.halted_at,
            last_rebalance_at=ts.isoformat(),
            consecutive_errors=state.consecutive_errors,
            positions=state.positions,
        )

    def reset_trailing_stops(
        self,
        state: PortfolioRiskState,
        stopped_ids: set[str],
        price_map: dict[str, Decimal],
    ) -> PortfolioRiskState:
        """Reset high_water_price to the current price for positions that triggered
        a trailing stop, so subsequent bars use the fresh baseline."""
        positions = dict(state.positions)
        for inst_id in stopped_ids:
            if inst_id in positions and inst_id in price_map:
                current_price = price_map[inst_id]
                positions[inst_id] = PositionState(
                    cost_basis=positions[inst_id].cost_basis,
                    high_water_price=current_price,
                    size=positions[inst_id].size,
                )
                self.logger.debug("Reset trailing stop for %s: high_water -> %s", inst_id, current_price)
        return PortfolioRiskState(
            equity_peak=state.equity_peak,
            trading_halted=state.trading_halted,
            halt_reason=state.halt_reason,
            halted_at=state.halted_at,
            last_rebalance_at=state.last_rebalance_at,
            consecutive_errors=state.consecutive_errors,
            positions=positions,
        )
