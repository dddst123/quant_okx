from __future__ import annotations

import unittest
from decimal import Decimal

from okx_quant.config import Settings
from okx_quant.models import AccountSnapshot, InstrumentRules, Signal, SignalAction
from okx_quant.risk import RiskManager


class RiskManagerTest(unittest.TestCase):
    def test_buy_intent_uses_base_currency_size(self) -> None:
        settings = Settings(trade_amount_quote=Decimal("50"), max_position_quote=Decimal("200"), min_cash_reserve_quote=Decimal("10"))
        rules = InstrumentRules(min_size=Decimal("0.0001"), lot_size=Decimal("0.0001"), tick_size=Decimal("0.1"))
        manager = RiskManager(settings, rules)
        signal = Signal(SignalAction.BUY, Decimal("10000"), "buy")
        snapshot = AccountSnapshot(base_balance=Decimal("0"), quote_balance=Decimal("100"), base_market_value=Decimal("0"))
        decision = manager.decide(signal, snapshot)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.intent.side, "buy")
        self.assertEqual(decision.intent.size, Decimal("0.0050"))

    def test_sell_rejected_when_balance_too_small(self) -> None:
        settings = Settings()
        rules = InstrumentRules(min_size=Decimal("0.001"), lot_size=Decimal("0.001"), tick_size=Decimal("0.1"))
        manager = RiskManager(settings, rules)
        signal = Signal(SignalAction.SELL, Decimal("10000"), "sell")
        snapshot = AccountSnapshot(base_balance=Decimal("0.0005"), quote_balance=Decimal("0"), base_market_value=Decimal("5"))
        decision = manager.decide(signal, snapshot)
        self.assertFalse(decision.approved)


if __name__ == "__main__":
    unittest.main()
