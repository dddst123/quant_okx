from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from okx_quant.models import Candle, SignalAction
from okx_quant.strategy.sma_cross import SmaCrossStrategy


class SmaCrossStrategyTest(unittest.TestCase):
    def _candles(self, closes: list[str]) -> list[Candle]:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = []
        for index, close in enumerate(closes):
            price = Decimal(close)
            candles.append(
                Candle(
                    ts=start + timedelta(minutes=index),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=Decimal("1"),
                    quote_volume=Decimal("1"),
                    confirmed=True,
                )
            )
        return candles

    def test_buy_signal_on_golden_cross(self) -> None:
        strategy = SmaCrossStrategy(3, 5)
        candles = self._candles(["10", "10", "10", "10", "10", "13"])
        signal = strategy.generate(candles)
        self.assertEqual(signal.action, SignalAction.BUY)

    def test_sell_signal_on_death_cross(self) -> None:
        strategy = SmaCrossStrategy(3, 5)
        candles = self._candles(["13", "13", "13", "13", "13", "10"])
        signal = strategy.generate(candles)
        self.assertEqual(signal.action, SignalAction.SELL)


if __name__ == "__main__":
    unittest.main()
