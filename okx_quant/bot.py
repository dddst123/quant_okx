from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from okx_quant.client import OkxRestClient
from okx_quant.config import Settings
from okx_quant.models import AccountSnapshot, Signal
from okx_quant.risk import RiskDecision, RiskManager
from okx_quant.strategy import SmaCrossStrategy


@dataclass(frozen=True)
class BotSnapshot:
    ts: datetime
    signal: Signal
    decision: RiskDecision | None
    order_result: dict | None
    price: Decimal
    base_balance: Decimal | None
    quote_balance: Decimal | None


class TradingBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OkxRestClient(settings)
        self.strategy = SmaCrossStrategy(settings.fast_window, settings.slow_window)
        self.logger = logging.getLogger("okx_quant.bot")
        self._last_trade_at: float = 0.0

    def _latest_signal(self) -> Signal:
        candles = self.client.get_candles(self.settings.instrument_id, self.settings.bar, self.settings.candles_limit)
        confirmed = [candle for candle in candles if candle.confirmed]
        return self.strategy.generate(confirmed)

    def get_signal_only(self) -> Signal:
        return self._latest_signal()

    def get_balances(self) -> dict[str, Decimal]:
        self.settings.require_private_api()
        return self.client.get_balances([self.settings.base_currency, self.settings.quote_currency])

    def run_once(self) -> BotSnapshot:
        self.settings.require_private_api()
        signal = self._latest_signal()
        balances = self.get_balances()
        snapshot = AccountSnapshot(
            base_balance=balances[self.settings.base_currency],
            quote_balance=balances[self.settings.quote_currency],
            base_market_value=balances[self.settings.base_currency] * signal.price,
        )
        rules = self.client.get_instrument_rules(self.settings.instrument_id)
        decision = RiskManager(self.settings, rules).decide(signal, snapshot)
        order_result = None

        if decision.approved and time.time() - self._last_trade_at < self.settings.cooldown_sec:
            decision = RiskDecision(False, None, "Cooldown window is active")

        if decision.approved and decision.intent:
            if self.settings.dry_run:
                self.logger.info("Dry-run: %s", decision.reason)
            else:
                order_result = self.client.place_market_order(
                    self.settings.instrument_id,
                    decision.intent.side,
                    decision.intent.size,
                    decision.intent.target_currency,
                )
                self._last_trade_at = time.time()
                self.logger.info("Order sent: %s", order_result)
        else:
            self.logger.info("Skipped: %s", decision.reason)

        return BotSnapshot(
            ts=datetime.now(UTC),
            signal=signal,
            decision=decision,
            order_result=order_result,
            price=signal.price,
            base_balance=snapshot.base_balance,
            quote_balance=snapshot.quote_balance,
        )

    def serve_forever(self) -> None:
        while True:
            try:
                snapshot = self.run_once()
                self.logger.info(
                    "Tick %s price=%s signal=%s base=%s quote=%s",
                    snapshot.ts.isoformat(),
                    snapshot.price,
                    snapshot.signal.action.value,
                    snapshot.base_balance,
                    snapshot.quote_balance,
                )
            except Exception:
                self.logger.exception("Trading loop failed")
            time.sleep(self.settings.poll_interval_sec)
