from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import tempfile
import unittest

from okx_quant.backtest import BacktestReport, EquityPoint
from okx_quant.config import Settings
from okx_quant.factor_bot import FactorRunSnapshot
from okx_quant.guardian import FactorGuardian, GuardianState, GuardianStateStore
from okx_quant.walk_forward import FactorWalkForwardAnalyzer


class GuardianStateStoreTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/guardian.json"
            store = GuardianStateStore(path)
            state = GuardianState(
                initial_equity_quote=Decimal("1234.5"),
                last_daily_summary_date="2026-03-30",
                last_trading_halted=True,
                last_halt_reason="max drawdown",
            )
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded.initial_equity_quote, Decimal("1234.5"))
            self.assertEqual(loaded.last_daily_summary_date, "2026-03-30")
            self.assertTrue(loaded.last_trading_halted)
            self.assertEqual(loaded.last_halt_reason, "max drawdown")


class DummyAlerts:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class DummyGuardianBot:
    def __init__(self, snapshots: list[FactorRunSnapshot]) -> None:
        self.snapshots = list(snapshots)

    def run_once(self) -> FactorRunSnapshot:
        if not self.snapshots:
            raise RuntimeError("No snapshots left")
        return self.snapshots.pop(0)


class FactorGuardianTest(unittest.TestCase):
    def test_guardian_logs_halt_and_resume_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                factor_guardian_output_dir=tmpdir,
                factor_guardian_state_path=f"{tmpdir}/guardian_state.json",
            )
            guardian = FactorGuardian(settings)
            halted_snapshot = FactorRunSnapshot(
                ts=datetime(2026, 3, 30, tzinfo=UTC),
                total_equity_quote=Decimal("9000"),
                available_quote=Decimal("4000"),
                drawdown=Decimal("0.10"),
                trading_halted=True,
                halt_reason="max drawdown 10.00% >= 8.00%",
                holdings={"BTC-USDT": Decimal("0.05")},
                holdings_quote={"BTC-USDT": Decimal("5000")},
                picks=[],
                planned_orders=[],
                executed_orders=[],
            )
            resumed_snapshot = FactorRunSnapshot(
                ts=datetime(2026, 3, 30, 1, tzinfo=UTC),
                total_equity_quote=Decimal("9300"),
                available_quote=Decimal("9300"),
                drawdown=Decimal("0.04"),
                trading_halted=False,
                halt_reason="",
                holdings={},
                holdings_quote={},
                picks=[],
                planned_orders=[],
                executed_orders=[],
            )
            guardian.bot = DummyGuardianBot([halted_snapshot, resumed_snapshot])
            guardian.alerts = DummyAlerts()

            guardian.run_once()
            guardian.run_once()

            events = [
                line.strip()
                for line in guardian.event_log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(events), 4)
            self.assertIn('"event_type": "tick"', events[0])
            self.assertIn('"event_type": "halted"', events[1])
            self.assertIn('"event_type": "tick"', events[2])
            self.assertIn('"event_type": "resumed"', events[3])

            alert_titles = [title for title, _ in guardian.alerts.messages]
            self.assertIn("Daily Guardian Summary", alert_titles)
            self.assertIn("Guardian Halt", alert_titles)
            self.assertIn("Guardian Resume", alert_titles)


class FactorWalkForwardAnalyzerTest(unittest.TestCase):
    def test_objective_caps_extreme_in_sample_returns(self) -> None:
        analyzer = FactorWalkForwardAnalyzer(Settings())
        extreme = BacktestReport(
            years=1,
            start="2024-01-01",
            end="2024-12-31",
            initial_capital="10000",
            ending_equity="25000",
            total_return=1.50,
            cagr=1.50,
            annualized_volatility=0.5,
            sharpe_ratio=1.0,
            max_drawdown=0.20,
            benchmark_return=0.5,
            total_trades=10,
            turnover_ratio=3.0,
            stop_loss_events=0,
            drawdown_halts=0,
            universe=["BTC-USDT"],
            picks_last=["BTC-USDT"],
            report_path="report.json",
            equity_curve_path="curve.csv",
        )
        moderate = BacktestReport(
            years=1,
            start="2024-01-01",
            end="2024-12-31",
            initial_capital="10000",
            ending_equity="22000",
            total_return=1.00,
            cagr=1.00,
            annualized_volatility=0.5,
            sharpe_ratio=1.0,
            max_drawdown=0.20,
            benchmark_return=0.5,
            total_trades=10,
            turnover_ratio=3.0,
            stop_loss_events=0,
            drawdown_halts=0,
            universe=["BTC-USDT"],
            picks_last=["BTC-USDT"],
            report_path="report.json",
            equity_curve_path="curve.csv",
        )
        equity_curve = [
            EquityPoint(datetime(2024, 1, 1, tzinfo=UTC), Decimal("10000"), Decimal("10000"), Decimal("0"), None),
            EquityPoint(datetime(2024, 7, 1, tzinfo=UTC), Decimal("18000"), Decimal("10000"), Decimal("0"), None),
            EquityPoint(datetime(2024, 12, 31, tzinfo=UTC), Decimal("22000"), Decimal("10000"), Decimal("0"), None),
        ]
        self.assertAlmostEqual(analyzer._objective(extreme), analyzer._objective(moderate), places=9)
        self.assertAlmostEqual(analyzer._objective(extreme, equity_curve), analyzer._objective(moderate, equity_curve), places=9)

    def test_objective_prefers_stronger_late_window(self) -> None:
        analyzer = FactorWalkForwardAnalyzer(Settings())
        report = BacktestReport(
            years=1,
            start="2024-01-01",
            end="2024-12-31",
            initial_capital="10000",
            ending_equity="15000",
            total_return=0.50,
            cagr=0.50,
            annualized_volatility=0.40,
            sharpe_ratio=0.80,
            max_drawdown=0.20,
            benchmark_return=0.1,
            total_trades=10,
            turnover_ratio=4.0,
            stop_loss_events=0,
            drawdown_halts=0,
            universe=["BTC-USDT"],
            picks_last=["BTC-USDT"],
            report_path="report.json",
            equity_curve_path="curve.csv",
        )
        start = datetime(2024, 1, 1, tzinfo=UTC)
        front_values = [
            Decimal("10000"),
            Decimal("11200"),
            Decimal("12400"),
            Decimal("13600"),
            Decimal("14800"),
            Decimal("16000"),
            Decimal("17200"),
            Decimal("16800"),
            Decimal("16400"),
            Decimal("16000"),
            Decimal("15600"),
            Decimal("15200"),
            Decimal("14800"),
            Decimal("14400"),
            Decimal("14000"),
            Decimal("13800"),
            Decimal("14000"),
            Decimal("14200"),
            Decimal("14400"),
            Decimal("14600"),
            Decimal("14700"),
            Decimal("14800"),
            Decimal("14900"),
            Decimal("14950"),
            Decimal("15000"),
        ]
        late_values = [
            Decimal("10000"),
            Decimal("10150"),
            Decimal("10300"),
            Decimal("10450"),
            Decimal("10600"),
            Decimal("10750"),
            Decimal("10900"),
            Decimal("11050"),
            Decimal("11200"),
            Decimal("11350"),
            Decimal("11500"),
            Decimal("11650"),
            Decimal("11800"),
            Decimal("12100"),
            Decimal("12400"),
            Decimal("12700"),
            Decimal("13000"),
            Decimal("13300"),
            Decimal("13600"),
            Decimal("13900"),
            Decimal("14200"),
            Decimal("14500"),
            Decimal("14750"),
            Decimal("14900"),
            Decimal("15000"),
        ]
        front_loaded = [
            EquityPoint(start + timedelta(days=15 * idx), equity, Decimal("10000"), Decimal("0"), None)
            for idx, equity in enumerate(front_values)
        ]
        late_strength = [
            EquityPoint(start + timedelta(days=15 * idx), equity, Decimal("10000"), Decimal("0"), None)
            for idx, equity in enumerate(late_values)
        ]
        self.assertGreater(analyzer._objective(report, late_strength), analyzer._objective(report, front_loaded))

    def test_stitch_equity_chains_windows(self) -> None:
        analyzer = FactorWalkForwardAnalyzer(Settings())
        start = datetime(2024, 1, 1, tzinfo=UTC)
        window_one = [
            EquityPoint(start, Decimal("10000"), Decimal("10000"), Decimal("0"), None),
            EquityPoint(start + timedelta(days=1), Decimal("11000"), Decimal("10000"), Decimal("0"), None),
        ]
        window_two = [
            EquityPoint(start + timedelta(days=2), Decimal("10000"), Decimal("10000"), Decimal("0"), None),
            EquityPoint(start + timedelta(days=3), Decimal("12000"), Decimal("10000"), Decimal("0"), None),
        ]
        total_return, max_drawdown = analyzer._stitch_equity(Decimal("10000"), [window_one, window_two])
        self.assertAlmostEqual(total_return, 0.32, places=6)
        self.assertAlmostEqual(max_drawdown, 0.0, places=6)

    def test_parameter_profiles_scale_search_space(self) -> None:
        analyzer = FactorWalkForwardAnalyzer(Settings())
        quick = analyzer._parameter_grid("quick")
        default = analyzer._parameter_grid("default")
        full = analyzer._parameter_grid("full")
        self.assertEqual(len(quick), 72)
        self.assertEqual(len(default), 384)
        self.assertEqual(len(full), 1920)
        self.assertLess(len(quick), len(default))
        self.assertLess(len(default), len(full))
        self.assertTrue(any(params["factor_dynamic_top_n_enabled"] for params in quick))
        self.assertTrue(any(not params["factor_dynamic_top_n_enabled"] for params in quick))
        self.assertTrue(any(not params["factor_market_state_enabled"] for params in quick))
        self.assertTrue(any(params["factor_market_state_enabled"] for params in quick))
        self.assertTrue(
            any(
                params["factor_dynamic_top_n_enabled"]
                and params["factor_dynamic_top_n_required_signals"] == 3
                and params["factor_dynamic_top_n_breadth_threshold"] == Decimal("0.45")
                and params["factor_market_state_enabled"]
                and params["factor_market_state_min_breadth"] == Decimal("0.25")
                for params in default
            )
        )
        self.assertTrue(
            any(
                params["factor_dynamic_top_n_enabled"]
                and params["factor_dynamic_top_n_required_signals"] == 4
                and params["factor_dynamic_top_n_breadth_threshold"] == Decimal("0.65")
                and params["factor_market_state_enabled"]
                and params["factor_market_state_min_benchmark_momentum"] == Decimal("0.03")
                for params in full
            )
        )


if __name__ == "__main__":
    unittest.main()
