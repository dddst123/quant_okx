from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from okx_quant.config import Settings
from okx_quant.dashboard import DashboardDataStore, _dashboard_html


class DashboardDataStoreTest(unittest.TestCase):
    def test_dashboard_html_includes_filter_persistence_and_grouping_hooks(self) -> None:
        html = _dashboard_html(20)

        self.assertIn("EVENT_FILTER_STORAGE_KEY", html)
        self.assertIn("EVENT_GROUP_STORAGE_KEY", html)
        self.assertIn("loadUiState()", html)
        self.assertIn('data-category="', html)
        self.assertIn("event-group", html)

    def test_snapshot_reads_latest_tick_and_backtest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            guardian_dir = Path(tmpdir) / "guardian"
            guardian_dir.mkdir(parents=True, exist_ok=True)
            event_log = guardian_dir / "factor_guardian_events.jsonl"
            event_log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event_type": "tick",
                                "ts": "2026-04-07T00:00:00+00:00",
                                "equity_quote": "10000",
                                "cash_quote": "6000",
                                "drawdown": "0.02",
                                "nav": 1.0,
                                "trading_halted": False,
                                "halt_reason": "",
                                "holdings": {"BTC-USDT": "0.10", "SOL-USDT": "0.50"},
                                "holdings_quote": {"BTC-USDT": "4000", "SOL-USDT": "50"},
                                "picks": ["BTC-USDT"],
                                "planned_orders": [],
                                "market_state": {
                                    "risk_score": 0.75,
                                    "exposure_multiplier": 0.60,
                                    "entries_allowed": True,
                                    "reduce_only": False,
                                    "reason": "healthy",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "halted",
                                "ts": "2026-04-07T01:00:00+00:00",
                                "halt_reason": "drawdown",
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "tick",
                                "ts": "2026-04-07T04:00:00+00:00",
                                "equity_quote": "10400",
                                "cash_quote": "5400",
                                "drawdown": "0.01",
                                "nav": 1.04,
                                "trading_halted": False,
                                "halt_reason": "",
                                "holdings": {"BTC-USDT": "0.10", "ETH-USDT": "0.20"},
                                "holdings_quote": {"BTC-USDT": "5000", "ETH-USDT": "400"},
                                "picks": ["BTC-USDT", "ETH-USDT"],
                                "planned_orders": [
                                    {
                                        "inst_id": "ETH-USDT",
                                        "side": "buy",
                                        "est_quote_value": "400",
                                        "reason": "buy to target ETH-USDT weight=50.00% score=1.2345",
                                    }
                                ],
                                "executed_orders": [{"instId": "ETH-USDT", "side": "buy", "ordId": "123"}],
                                "market_state": {
                                    "risk_score": 0.82,
                                    "exposure_multiplier": 0.75,
                                    "entries_allowed": True,
                                    "reduce_only": False,
                                    "reason": "strong breadth",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "tick",
                                "ts": "2026-04-07T08:00:00+00:00",
                                "equity_quote": "10100",
                                "cash_quote": "5900",
                                "drawdown": "0.03",
                                "nav": 1.01,
                                "trading_halted": False,
                                "halt_reason": "",
                                "holdings": {"BTC-USDT": "0.07", "ETH-USDT": "0.15"},
                                "holdings_quote": {"BTC-USDT": "2800", "ETH-USDT": "300"},
                                "picks": ["ETH-USDT"],
                                "planned_orders": [
                                    {
                                        "inst_id": "BTC-USDT",
                                        "side": "sell",
                                        "est_quote_value": "2200",
                                        "reason": "stop-loss trim BTC-USDT after momentum break",
                                    }
                                ],
                                "executed_orders": [{"instId": "BTC-USDT", "side": "sell", "ordId": "124"}],
                                "market_state": {
                                    "risk_score": 0.91,
                                    "exposure_multiplier": 0.35,
                                    "entries_allowed": False,
                                    "reduce_only": True,
                                    "reason": "risk trim",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "event_type": "halted",
                                "ts": "2026-04-07T09:00:00+00:00",
                                "halt_reason": "max drawdown 3.00% >= 2.50%",
                                "equity_quote": "10100",
                                "drawdown": "0.03",
                                "nav": 1.01,
                                "holdings_quote": {"BTC-USDT": "2800", "ETH-USDT": "300"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            backtest_dir = Path(tmpdir) / "backtests"
            backtest_dir.mkdir(parents=True, exist_ok=True)
            (backtest_dir / "factor_backtest_1y_test.json").write_text(
                json.dumps(
                    {
                        "years": 1,
                        "start": "2025-04-07",
                        "end": "2026-04-07",
                        "total_return": 0.12,
                        "cagr": 0.12,
                        "sharpe_ratio": 0.9,
                        "max_drawdown": 0.15,
                        "benchmark_return": 0.05,
                        "total_trades": 88,
                        "turnover_ratio": 14.2,
                        "picks_last": ["BTC-USDT", "ETH-USDT"],
                    }
                ),
                encoding="utf-8",
            )

            settings = Settings(
                factor_guardian_output_dir=str(guardian_dir),
                factor_backtest_output_dir=str(backtest_dir),
                factor_state_path=str(Path(tmpdir) / "state.json"),
            )
            store = DashboardDataStore(settings)
            snapshot = store.snapshot(limit=10)

            self.assertTrue(snapshot["status"]["has_guardian_data"])
            self.assertEqual(snapshot["status"]["curve_points"], 3)
            self.assertEqual(snapshot["latest_tick"]["equity_quote"], "10100")
            self.assertEqual(snapshot["latest_transition"]["event_type"], "halted")
            self.assertEqual(snapshot["curve"][-1]["equity_quote"], 10100.0)
            self.assertEqual(snapshot["curve"][-1]["drawdown"], 0.03)
            self.assertEqual(len(snapshot["recent_rebalances"]), 2)
            self.assertEqual(snapshot["recent_rebalances"][0]["status"], "filled")
            self.assertEqual(snapshot["recent_rebalances"][0]["orders"][0]["inst_id"], "BTC-USDT")
            self.assertEqual(snapshot["recent_rebalances"][0]["orders"][0]["event_kind"], "stop-loss")
            self.assertEqual(snapshot["latest_rebalance"]["ts"], "2026-04-07T08:00:00+00:00")
            self.assertEqual(snapshot["latest_highlight"]["category"], "circuit-breaker")
            self.assertEqual(snapshot["latest_highlight"]["kind"], "halted")
            self.assertEqual(snapshot["latest_highlight"]["title"], "Circuit breaker triggered")
            self.assertEqual(snapshot["recent_events"][0]["category"], "circuit-breaker")
            self.assertEqual(snapshot["recent_events"][1]["category"], "stop-loss")
            latest_changes = {
                item["inst_id"]: item for item in snapshot["recent_rebalances"][0]["holding_changes"]
            }
            self.assertAlmostEqual(snapshot["recent_rebalances"][0]["before_holdings_quote"], 5400.0)
            self.assertAlmostEqual(snapshot["recent_rebalances"][0]["after_holdings_quote"], 3100.0)
            self.assertAlmostEqual(latest_changes["BTC-USDT"]["delta_quote"], -2200.0)
            self.assertAlmostEqual(latest_changes["ETH-USDT"]["delta_quote"], -100.0)
            self.assertEqual(latest_changes["BTC-USDT"]["position_action"], "decrease")
            older_changes = {
                item["inst_id"]: item for item in snapshot["recent_rebalances"][1]["holding_changes"]
            }
            self.assertAlmostEqual(older_changes["ETH-USDT"]["delta_quote"], 400.0)
            self.assertEqual(older_changes["ETH-USDT"]["position_action"], "increase")
            self.assertEqual(older_changes["SOL-USDT"]["position_action"], "clear")
            self.assertEqual(snapshot["latest_backtest"]["total_trades"], 88)
            self.assertEqual(snapshot["latest_backtest"]["picks_last"], ["BTC-USDT", "ETH-USDT"])

    def test_snapshot_handles_missing_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                factor_guardian_output_dir=str(Path(tmpdir) / "guardian"),
                factor_backtest_output_dir=str(Path(tmpdir) / "backtests"),
                factor_state_path=str(Path(tmpdir) / "state.json"),
            )
            store = DashboardDataStore(settings)
            snapshot = store.snapshot(limit=20)

            self.assertFalse(snapshot["status"]["has_guardian_data"])
            self.assertEqual(snapshot["curve"], [])
            self.assertIsNone(snapshot["latest_tick"])
            self.assertIsNone(snapshot["latest_rebalance"])
            self.assertIsNone(snapshot["latest_highlight"])
            self.assertEqual(snapshot["recent_events"], [])
            self.assertEqual(snapshot["recent_rebalances"], [])
            self.assertIsNone(snapshot["latest_backtest"])


if __name__ == "__main__":
    unittest.main()
