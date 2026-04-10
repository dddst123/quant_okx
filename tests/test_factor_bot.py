from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from okx_quant.client import OkxApiError
from okx_quant.config import Settings
from okx_quant.factor_bot import FactorPortfolioBot, FactorRunSnapshot, PlannedOrder
from okx_quant.models import SpotTicker


class FactorPortfolioBotTest(unittest.TestCase):
    def _snapshot(self) -> FactorRunSnapshot:
        return FactorRunSnapshot(
            ts=datetime(2026, 4, 10, tzinfo=UTC),
            total_equity_quote=Decimal("100"),
            available_quote=Decimal("100"),
            drawdown=Decimal("0"),
            trading_halted=False,
            halt_reason="",
            holdings={},
            holdings_quote={},
            picks=[],
            planned_orders=[],
            executed_orders=[],
            market_state=None,
        )

    def test_serve_forever_initializes_interval_sleep_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                api_key="key",
                api_secret="secret",
                api_passphrase="passphrase",
                factor_state_path=str(Path(tmpdir) / "factor_state.json"),
                factor_rebalance_mode="interval",
                factor_rebalance_interval_sec=60,
            )
            bot = FactorPortfolioBot(settings)
            bot.run_once = Mock(return_value=self._snapshot())  # type: ignore[method-assign]

            with patch("okx_quant.factor_bot.time.sleep", side_effect=StopIteration), self.assertRaises(StopIteration):
                bot.serve_forever()

            bot.run_once.assert_called_once()

    def test_run_once_refreshes_balances_and_keeps_rebalance_pending_after_fill_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                api_key="key",
                api_secret="secret",
                api_passphrase="passphrase",
                dry_run=False,
                factor_state_path=str(Path(tmpdir) / "factor_state.json"),
            )
            bot = FactorPortfolioBot(settings)
            ticker = SpotTicker(
                inst_id="BTC-USDT",
                last=Decimal("100"),
                quote_volume_24h=Decimal("1000000"),
                base_volume_24h=Decimal("10000"),
            )
            planned_order = PlannedOrder(
                inst_id="BTC-USDT",
                side="buy",
                size=Decimal("1"),
                target_currency="base_ccy",
                est_quote_value=Decimal("100"),
                reason="buy to target BTC-USDT weight=100.00% score=1.2345",
            )

            bot._ticker_map = Mock(return_value={"BTC-USDT": ticker})  # type: ignore[method-assign]
            bot._rebalance_due = Mock(return_value=True)  # type: ignore[method-assign]
            bot._discover_universe = Mock(return_value=["BTC-USDT"])  # type: ignore[method-assign]
            bot._fetch_market_data = Mock(return_value={"BTC-USDT": []})  # type: ignore[method-assign]
            bot._ensure_benchmark_data = Mock(return_value=[])  # type: ignore[method-assign]
            bot._build_market_state = Mock(return_value=None)  # type: ignore[method-assign]
            bot._build_orders = Mock(
                return_value=(Decimal("100"), Decimal("100"), [planned_order])
            )  # type: ignore[method-assign]
            bot.strategy.evaluate = Mock(return_value=[])  # type: ignore[method-assign]
            bot.client.get_all_balances = Mock(
                side_effect=[
                    {"USDT": Decimal("100")},
                    {"USDT": Decimal("0"), "BTC": Decimal("1")},
                ]
            )
            bot.client.place_market_order = Mock(return_value={"clOrdId": "abc"})
            bot.client.wait_for_fill = Mock(side_effect=OkxApiError("timeout"))

            snapshot = bot.run_once()
            saved_state = bot.state_store.load()

            self.assertEqual(bot.client.get_all_balances.call_count, 2)
            self.assertEqual(saved_state.last_rebalance_at, "")
            self.assertEqual(saved_state.positions["BTC-USDT"].size, Decimal("1"))
            self.assertEqual(snapshot.available_quote, Decimal("0"))
            self.assertEqual(snapshot.holdings, {"BTC-USDT": Decimal("1")})
            self.assertEqual(snapshot.executed_orders, [])


if __name__ == "__main__":
    unittest.main()
