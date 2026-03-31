from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from okx_quant.config import Settings
from okx_quant.models import AccountSnapshot, InstrumentRules, OrderIntent, Signal, SignalAction


@dataclass
class RiskDecision:
    approved: bool
    intent: OrderIntent | None
    reason: str


class RiskManager:
    def __init__(self, settings: Settings, rules: InstrumentRules) -> None:
        self.settings = settings
        self.rules = rules

    def _round_down(self, value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        units = (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN)
        return units * step

    def decide(self, signal: Signal, snapshot: AccountSnapshot) -> RiskDecision:
        if signal.action == SignalAction.HOLD:
            return RiskDecision(False, None, signal.reason)

        minimum_notional = self.rules.min_size * signal.price

        if signal.action == SignalAction.BUY:
            available_quote = snapshot.quote_balance - self.settings.min_cash_reserve_quote
            position_capacity = self.settings.max_position_quote - snapshot.base_market_value
            budget = min(self.settings.trade_amount_quote, available_quote, position_capacity)
            if budget <= 0:
                return RiskDecision(False, None, "No quote balance available after reserve check")
            base_size = self._round_down(budget / signal.price, self.rules.lot_size)
            if base_size < self.rules.min_size or budget < minimum_notional:
                return RiskDecision(False, None, "Computed buy size is below exchange minimum")
            return RiskDecision(
                True,
                OrderIntent(
                    side="buy",
                    size=base_size,
                    target_currency="base_ccy",
                    reason=signal.reason,
                ),
                f"Buy {base_size} {self.settings.base_currency}",
            )

        sell_size = self._round_down(snapshot.base_balance, self.rules.lot_size)
        if sell_size < self.rules.min_size:
            return RiskDecision(False, None, "Base balance is below exchange minimum")
        return RiskDecision(
            True,
            OrderIntent(
                side="sell",
                size=sell_size,
                target_currency="base_ccy",
                reason=signal.reason,
            ),
            f"Sell {sell_size} {self.settings.base_currency}",
        )
