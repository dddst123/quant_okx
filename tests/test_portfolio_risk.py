from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from okx_quant.backtest import FactorBacktester
from okx_quant.config import Settings
from okx_quant.models import Candle, SpotTicker
from okx_quant.portfolio_risk import (
    PortfolioRiskEngine,
    PortfolioRiskState,
    PortfolioStateStore,
    PositionState,
)


class FakeBacktestClient:
    def __init__(self, history: dict[str, list[Candle]]) -> None:
        self.history = history

    def get_spot_tickers(self) -> list[SpotTicker]:
        tickers = []
        for inst_id, candles in self.history.items():
            last = candles[-1]
            tickers.append(
                SpotTicker(
                    inst_id=inst_id,
                    last=last.close,
                    quote_volume_24h=last.quote_volume,
                    base_volume_24h=last.volume,
                )
            )
        return tickers

    def get_history_candles_paginated(self, inst_id: str, bar: str, limit: int) -> list[Candle]:
        return self.history[inst_id][-limit:]


class PortfolioRiskEngineTest(unittest.TestCase):
    def test_stop_decisions_and_drawdown_halt(self) -> None:
        settings = Settings(
            factor_stop_loss_pct=Decimal("0.10"),
            factor_trailing_stop_pct=Decimal("0.15"),
            factor_max_drawdown_pct=Decimal("0.20"),
        )
        engine = PortfolioRiskEngine(settings)
        state = PortfolioRiskState(
            equity_peak=Decimal("1000"),
            positions={"BTC-USDT": PositionState(cost_basis=Decimal("100"), high_water_price=Decimal("130"))},
        )
        decisions = engine.stop_decisions(state, {"BTC-USDT": Decimal("1")}, {"BTC-USDT": Decimal("85")})
        self.assertEqual(len(decisions), 1)
        updated, drawdown, triggered = engine.apply_drawdown(state, Decimal("790"))
        self.assertTrue(triggered)
        self.assertTrue(updated.trading_halted)
        self.assertGreaterEqual(drawdown, Decimal("0.20"))

    def test_state_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PortfolioStateStore(f"{tmpdir}/state.json")
            state = PortfolioRiskState(
                equity_peak=Decimal("123"),
                positions={"ETH-USDT": PositionState(cost_basis=Decimal("10"), high_water_price=Decimal("12"))},
            )
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded.equity_peak, Decimal("123"))
            self.assertIn("ETH-USDT", loaded.positions)

    def test_halt_can_resume_after_cooldown_when_benchmark_recovers(self) -> None:
        settings = Settings(
            factor_max_drawdown_pct=Decimal("0.20"),
            factor_halt_cooldown_days=7,
            factor_halt_resume_requires_benchmark_trend=True,
        )
        engine = PortfolioRiskEngine(settings)
        state = PortfolioRiskState(
            equity_peak=Decimal("1000"),
            trading_halted=True,
            halt_reason="drawdown",
            halted_at=datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
        )
        resumed, changed = engine.maybe_resume(
            state,
            datetime(2024, 1, 10, tzinfo=UTC),
            Decimal("800"),
            benchmark_trend_on=True,
        )
        self.assertTrue(changed)
        self.assertFalse(resumed.trading_halted)
        self.assertEqual(resumed.equity_peak, Decimal("800"))

    def test_exposure_multiplier_uses_drawdown_tiers(self) -> None:
        settings = Settings(
            factor_drawdown_scale_tiers=((Decimal("0.10"), Decimal("0.8")), (Decimal("0.20"), Decimal("0.5"))),
        )
        engine = PortfolioRiskEngine(settings)
        self.assertEqual(engine.exposure_multiplier(Decimal("0.05")), Decimal("1"))
        self.assertEqual(engine.exposure_multiplier(Decimal("0.12")), Decimal("0.8"))
        self.assertEqual(engine.exposure_multiplier(Decimal("0.25")), Decimal("0.5"))


class FactorBacktesterTest(unittest.TestCase):
    def _candles(self, start_price: str, step: str, count: int = 420) -> list[Candle]:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        price = Decimal(start_price)
        step_decimal = Decimal(step)
        candles: list[Candle] = []
        for index in range(count):
            close = price
            candles.append(
                Candle(
                    ts=start + timedelta(days=index),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=Decimal("100"),
                    quote_volume=Decimal("100") * close,
                    confirmed=True,
                )
            )
            price += step_decimal
        return candles

    def test_backtest_runs_on_synthetic_history(self) -> None:
        history = {
            "BTC-USDT": self._candles("100", "0.8"),
            "ETH-USDT": self._candles("50", "1.0"),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                factor_universe=("BTC-USDT", "ETH-USDT"),
                factor_benchmark_inst_id="BTC-USDT",
                factor_min_history=90,
                factor_min_24h_quote_volume=Decimal("1000"),
                factor_backtest_output_dir=tmpdir,
                factor_backtest_initial_capital=Decimal("10000"),
                factor_top_n=1,
                factor_backtest_fee_rate=Decimal("0.000"),
                factor_backtest_slippage_rate=Decimal("0.000"),
            )
            backtester = FactorBacktester(settings)
            backtester.client = FakeBacktestClient(history)  # type: ignore[assignment]
            report = backtester.run(1)
            self.assertEqual(report.years, 1)
            self.assertGreater(report.total_trades, 0)
            self.assertGreater(report.total_return, 0)


if __name__ == "__main__":
    unittest.main()
