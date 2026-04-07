from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from okx_quant.config import Settings
from okx_quant.factor_bot import FactorPortfolioBot
from okx_quant.portfolio_risk import PortfolioRiskState
from okx_quant.timeframe import bar_seconds, bars_for_days, bars_per_year
from okx_quant.walk_forward import FactorWalkForwardAnalyzer


class TimeframeHelperTest(unittest.TestCase):
    def test_bar_helpers_support_intraday_and_utc_bars(self) -> None:
        self.assertEqual(bar_seconds("4H"), 4 * 60 * 60)
        self.assertEqual(bar_seconds("1Dutc"), 24 * 60 * 60)
        self.assertEqual(bars_for_days("4H", 10), 60)
        self.assertAlmostEqual(bars_per_year("4H"), 365 * 6, places=6)

    def test_interval_rebalance_mode_requires_bar_alignment(self) -> None:
        Settings(
            factor_bar="4H",
            factor_rebalance_mode="interval",
            factor_rebalance_interval_sec=4 * 60 * 60,
        ).validate()
        with self.assertRaises(ValueError):
            Settings(
                factor_bar="4H",
                factor_rebalance_mode="interval",
                factor_rebalance_interval_sec=60 * 60,
            ).validate()

    def test_factor_bot_interval_rebalance_waits_for_elapsed_interval(self) -> None:
        settings = Settings(
            factor_bar="4H",
            factor_rebalance_mode="interval",
            factor_rebalance_interval_sec=4 * 60 * 60,
        )
        bot = FactorPortfolioBot(settings)
        last = datetime(2026, 4, 7, 0, 0, tzinfo=UTC)
        state = PortfolioRiskState(last_rebalance_at=last.isoformat())
        self.assertFalse(bot._rebalance_due(state, last + timedelta(hours=3, minutes=59)))
        self.assertTrue(bot._rebalance_due(state, last + timedelta(hours=4)))

    def test_walk_forward_grid_switches_to_interval_mode_for_intraday_bars(self) -> None:
        analyzer = FactorWalkForwardAnalyzer(
            Settings(
                factor_bar="4H",
                factor_rebalance_mode="interval",
                factor_rebalance_interval_sec=4 * 60 * 60,
            )
        )
        quick = analyzer._parameter_grid("quick")
        self.assertTrue(quick)
        self.assertTrue(all(params["factor_rebalance_mode"] == "interval" for params in quick))
        self.assertTrue(all(params["factor_rebalance_interval_sec"] == 4 * 60 * 60 for params in quick))


if __name__ == "__main__":
    unittest.main()
