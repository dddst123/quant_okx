from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from okx_quant.config import Settings


@dataclass
class PositionState:
    entry_price: Decimal
    high_water_price: Decimal


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


class PortfolioStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> PortfolioRiskState:
        if not self.path.exists():
            return PortfolioRiskState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        positions = {
            inst_id: PositionState(
                entry_price=Decimal(item["entry_price"]),
                high_water_price=Decimal(item["high_water_price"]),
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
                    "entry_price": str(item.entry_price),
                    "high_water_price": str(item.high_water_price),
                }
                for inst_id, item in state.positions.items()
            },
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    def reset(self) -> PortfolioRiskState:
        state = PortfolioRiskState()
        self.save(state)
        return state


class PortfolioRiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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
                positions[inst_id] = PositionState(entry_price=price, high_water_price=price)
            else:
                positions[inst_id] = PositionState(
                    entry_price=existing.entry_price,
                    high_water_price=max(existing.high_water_price, price),
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

        halted_at = datetime.fromisoformat(state.halted_at)
        if ts < halted_at + timedelta(days=self.settings.factor_halt_cooldown_days):
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
        decisions: list[StopDecision] = []
        for inst_id, size in holdings.items():
            if size <= 0 or inst_id not in price_map:
                continue
            position = state.positions.get(inst_id)
            if position is None:
                continue
            price = price_map[inst_id]
            if self.settings.factor_stop_loss_pct > 0:
                stop_price = position.entry_price * (Decimal("1") - self.settings.factor_stop_loss_pct)
                if price <= stop_price:
                    decisions.append(StopDecision(inst_id, f"hard stop hit at {price} <= {stop_price}"))
                    continue
            if self.settings.factor_trailing_stop_pct > 0:
                trailing_price = position.high_water_price * (Decimal("1") - self.settings.factor_trailing_stop_pct)
                if price <= trailing_price:
                    decisions.append(StopDecision(inst_id, f"trailing stop hit at {price} <= {trailing_price}"))
        return decisions

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
