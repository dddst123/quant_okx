from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from okx_quant.config import Settings
from okx_quant.market_state import MarketStateEngine, PublicMarketStateSnapshot
from okx_quant.models import Candle


class MarketStateEngineTest(unittest.TestCase):
    def _candles(
        self,
        start_price: str,
        step: str,
        *,
        count: int = 160,
        volume: str = "100",
        final_volume: str | None = None,
        wiggle: str = "0",
    ) -> list[Candle]:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        price = Decimal(start_price)
        step_decimal = Decimal(step)
        wiggle_decimal = Decimal(wiggle)
        candles: list[Candle] = []
        for index in range(count):
            offset = Decimal((index % 7) - 3) * wiggle_decimal
            close = price + offset
            current_volume = Decimal(final_volume if final_volume and index == count - 1 else volume)
            candles.append(
                Candle(
                    ts=start + timedelta(days=index),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=current_volume,
                    quote_volume=current_volume * close,
                    confirmed=True,
                )
            )
            price += step_decimal
        return candles

    def test_snapshot_stays_open_when_market_state_is_healthy(self) -> None:
        settings = Settings(
            factor_min_history=120,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_volume_lookback=20,
            factor_volatility_lookback=20,
            factor_market_state_min_breadth=Decimal("0.25"),
            factor_market_state_max_spread_bps=Decimal("8"),
            factor_market_state_min_depth_quote=Decimal("25000"),
            factor_market_state_max_abs_funding_rate=Decimal("0.0015"),
        )
        engine = MarketStateEngine(settings)
        benchmark = self._candles("100", "1.2", final_volume="500", wiggle="0.3")
        market_data = {
            "BTC-USDT": benchmark,
            "ETH-USDT": self._candles("100", "2.0", final_volume="450", wiggle="1.0"),
            "SOL-USDT": self._candles("100", "1.5", final_volume="350", wiggle="0.8"),
            "DOGE-USDT": self._candles("100", "1.0", final_volume="250", wiggle="0.9"),
        }
        public_state = PublicMarketStateSnapshot(
            benchmark_inst_id="BTC-USDT",
            benchmark_swap_inst_id="BTC-USDT-SWAP",
            spread_bps=1.2,
            bid_depth_quote=Decimal("50000"),
            ask_depth_quote=Decimal("48000"),
            funding_rate=0.0001,
            open_interest_usd=Decimal("1000000000"),
            ts=benchmark[-1].ts,
            notes=(),
        )
        snapshot = engine.snapshot(market_data, benchmark, public_state=public_state)
        self.assertTrue(snapshot.entries_allowed)
        self.assertFalse(snapshot.reduce_only)
        self.assertGreaterEqual(snapshot.risk_score, 0.99)
        self.assertAlmostEqual(snapshot.exposure_multiplier, 1.0, places=6)
        self.assertEqual(snapshot.reason, "healthy")

    def test_snapshot_blocks_entries_when_microstructure_is_stressed(self) -> None:
        settings = Settings(
            factor_min_history=120,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_volume_lookback=20,
            factor_volatility_lookback=20,
            factor_market_state_min_breadth=Decimal("0.25"),
            factor_market_state_max_spread_bps=Decimal("5"),
            factor_market_state_min_depth_quote=Decimal("30000"),
            factor_market_state_max_abs_funding_rate=Decimal("0.0010"),
            factor_market_state_min_open_interest_usd=Decimal("500000000"),
            factor_market_state_entry_gate=Decimal("0.60"),
        )
        engine = MarketStateEngine(settings)
        benchmark = self._candles("100", "1.0", final_volume="450", wiggle="0.4")
        market_data = {
            "BTC-USDT": benchmark,
            "ETH-USDT": self._candles("100", "2.0", final_volume="420", wiggle="1.0"),
            "SOL-USDT": self._candles("100", "1.4", final_volume="320", wiggle="1.2"),
        }
        public_state = PublicMarketStateSnapshot(
            benchmark_inst_id="BTC-USDT",
            benchmark_swap_inst_id="BTC-USDT-SWAP",
            spread_bps=18.0,
            bid_depth_quote=Decimal("12000"),
            ask_depth_quote=Decimal("15000"),
            funding_rate=0.0025,
            open_interest_usd=Decimal("200000000"),
            ts=benchmark[-1].ts,
            notes=("test",),
        )
        snapshot = engine.snapshot(market_data, benchmark, public_state=public_state)
        self.assertFalse(snapshot.entries_allowed)
        self.assertTrue(snapshot.reduce_only)
        self.assertLess(snapshot.risk_score, 0.60)
        self.assertLess(snapshot.exposure_multiplier, 1.0)
        self.assertIn("spread_bps", snapshot.reason)
        self.assertIn("depth_quote", snapshot.reason)
        self.assertIn("funding_rate", snapshot.reason)
        self.assertIn("open_interest_usd", snapshot.reason)


if __name__ == "__main__":
    unittest.main()
