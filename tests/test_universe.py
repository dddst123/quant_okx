from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from okx_quant.config import Settings
from okx_quant.models import Candle, SpotTicker
from okx_quant.universe import discover_factor_universe


class DiscoverFactorUniverseTest(unittest.TestCase):
    def _candles(self, quote_volume: str, *, count: int = 80) -> list[Candle]:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles: list[Candle] = []
        for index in range(count):
            price = Decimal("10")
            volume = Decimal(quote_volume) / price
            candles.append(
                Candle(
                    ts=start + timedelta(days=index),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=volume,
                    quote_volume=Decimal(quote_volume),
                    confirmed=True,
                )
            )
        return candles

    def test_discovery_keeps_historically_liquid_assets_even_if_current_volume_is_soft(self) -> None:
        settings = Settings(
            factor_quote_currency="USDT",
            factor_min_last_price=Decimal("0.05"),
            factor_min_24h_quote_volume=Decimal("20000000"),
            factor_min_history=60,
            factor_liquidity_lookback=30,
            factor_universe_candidates=4,
            factor_max_universe_size=3,
        )
        ticker_map = {
            "BTC-USDT": SpotTicker("BTC-USDT", Decimal("100"), Decimal("50000000"), Decimal("1")),
            "ETH-USDT": SpotTicker("ETH-USDT", Decimal("90"), Decimal("45000000"), Decimal("1")),
            # This name should still survive because its trailing median liquidity is healthy.
            "LINK-USDT": SpotTicker("LINK-USDT", Decimal("20"), Decimal("5000000"), Decimal("1")),
            # This one should be filtered after the historical check.
            "JUNK-USDT": SpotTicker("JUNK-USDT", Decimal("2"), Decimal("40000000"), Decimal("1")),
        }
        history = {
            "BTC-USDT": self._candles("50000000"),
            "ETH-USDT": self._candles("45000000"),
            "LINK-USDT": self._candles("25000000"),
            "JUNK-USDT": self._candles("2000000"),
        }

        universe = discover_factor_universe(
            settings,
            ticker_map,
            set(ticker_map),
            lambda inst_id, limit: history[inst_id][-limit:],
        )

        self.assertEqual(universe, ["BTC-USDT", "ETH-USDT", "LINK-USDT"])

    def test_discovery_respects_history_requirement(self) -> None:
        settings = Settings(
            factor_quote_currency="USDT",
            factor_min_24h_quote_volume=Decimal("20000000"),
            factor_min_history=60,
            factor_liquidity_lookback=30,
            factor_universe_candidates=3,
            factor_max_universe_size=3,
        )
        ticker_map = {
            "BTC-USDT": SpotTicker("BTC-USDT", Decimal("100"), Decimal("50000000"), Decimal("1")),
            "NEW-USDT": SpotTicker("NEW-USDT", Decimal("15"), Decimal("30000000"), Decimal("1")),
            "ETH-USDT": SpotTicker("ETH-USDT", Decimal("90"), Decimal("45000000"), Decimal("1")),
        }
        history = {
            "BTC-USDT": self._candles("50000000"),
            "NEW-USDT": self._candles("30000000", count=20),
            "ETH-USDT": self._candles("45000000"),
        }

        universe = discover_factor_universe(
            settings,
            ticker_map,
            set(ticker_map),
            lambda inst_id, limit: history[inst_id][-limit:],
        )

        self.assertEqual(universe, ["BTC-USDT", "ETH-USDT"])


if __name__ == "__main__":
    unittest.main()
