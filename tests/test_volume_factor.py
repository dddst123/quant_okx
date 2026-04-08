from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from okx_quant.config import Settings
from okx_quant.factor_bot import FactorPortfolioBot
from okx_quant.market_state import MarketStateSnapshot
from okx_quant.models import Candle, InstrumentRules, SpotTicker
from okx_quant.strategy.volume_trend_factor import FactorCandidate, VolumeTrendFactorStrategy


class VolumeTrendFactorStrategyTest(unittest.TestCase):
    def _market_state(
        self,
        *,
        exposure_multiplier: float = 1.0,
        entries_allowed: bool = True,
        reason: str = "healthy",
    ) -> MarketStateSnapshot:
        return MarketStateSnapshot(
            benchmark_inst_id="BTC-USDT",
            benchmark_swap_inst_id="BTC-USDT-SWAP",
            ts=datetime(2024, 6, 1, tzinfo=UTC),
            breadth=0.60,
            benchmark_momentum=0.12,
            benchmark_slow_gap=0.08,
            benchmark_volatility=0.02,
            momentum_dispersion=0.10,
            median_volume_ratio=1.10,
            spread_bps=1.0,
            bid_depth_quote=Decimal("50000"),
            ask_depth_quote=Decimal("50000"),
            funding_rate=0.0001,
            open_interest_usd=Decimal("1000000000"),
            risk_score=0.90 if entries_allowed else 0.40,
            exposure_multiplier=exposure_multiplier,
            entries_allowed=entries_allowed,
            reduce_only=not entries_allowed,
            reason=reason,
            notes=(),
        )

    def _candles(
        self,
        start_price: str,
        step: str,
        count: int = 100,
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

    def test_evaluate_prefers_strong_liquid_trends(self) -> None:
        settings = Settings(
            factor_top_n=2,
            factor_max_asset_weight=Decimal("0.70"),
            factor_min_history=90,
        )
        strategy = VolumeTrendFactorStrategy(settings)
        market_data = {
            "BTC-USDT": self._candles("100", "2", final_volume="400"),
            "ETH-USDT": self._candles("100", "1"),
            "DOGE-USDT": self._candles("100", "-1"),
        }
        picks = strategy.evaluate(market_data)
        self.assertEqual({pick.inst_id for pick in picks}, {"BTC-USDT", "ETH-USDT"})
        self.assertGreater(picks[0].score, 0)
        self.assertLessEqual(sum(pick.weight for pick in picks), 1.0)

    def test_weights_respect_cap(self) -> None:
        settings = Settings(
            factor_top_n=3,
            factor_max_asset_weight=Decimal("0.45"),
            factor_min_history=90,
        )
        strategy = VolumeTrendFactorStrategy(settings)
        market_data = {
            "BTC-USDT": self._candles("100", "2", final_volume="400"),
            "ETH-USDT": self._candles("100", "1.8", final_volume="300"),
            "SOL-USDT": self._candles("100", "1.4", final_volume="250"),
        }
        picks = strategy.evaluate(market_data)
        self.assertTrue(picks)
        for pick in picks:
            self.assertLessEqual(pick.weight, 0.45 + 1e-9)

    def test_two_pick_portfolio_respects_cap_and_keeps_cash_buffer(self) -> None:
        settings = Settings(
            factor_top_n=2,
            factor_max_asset_weight=Decimal("0.45"),
            factor_min_history=90,
        )
        strategy = VolumeTrendFactorStrategy(settings)
        market_data = {
            "BTC-USDT": self._candles("100", "2", final_volume="400"),
            "ETH-USDT": self._candles("100", "1.8", final_volume="300"),
            "SOL-USDT": self._candles("100", "-1"),
        }
        picks = strategy.evaluate(market_data)
        self.assertEqual(len(picks), 2)
        self.assertTrue(all(pick.weight <= 0.45 + 1e-9 for pick in picks))
        self.assertLessEqual(sum(pick.weight for pick in picks), 0.90 + 1e-9)

    def test_single_pick_can_fully_deploy(self) -> None:
        settings = Settings(
            factor_top_n=1,
            factor_max_asset_weight=Decimal("0.45"),
            factor_min_history=90,
        )
        strategy = VolumeTrendFactorStrategy(settings)
        market_data = {
            "BTC-USDT": self._candles("100", "2", final_volume="400"),
            "ETH-USDT": self._candles("100", "-1"),
        }
        picks = strategy.evaluate(market_data)
        self.assertEqual([pick.inst_id for pick in picks], ["BTC-USDT"])
        self.assertAlmostEqual(picks[0].weight, 1.0, places=6)

    def test_strong_regime_expands_selection_to_dynamic_top_n(self) -> None:
        settings = Settings(
            factor_top_n=1,
            factor_dynamic_top_n_enabled=True,
            factor_dynamic_top_n=2,
            factor_dynamic_top_n_required_signals=4,
            factor_dynamic_top_n_breadth_threshold=Decimal("0.60"),
            factor_dynamic_top_n_benchmark_momentum=Decimal("0.08"),
            factor_max_asset_weight=Decimal("0.45"),
            factor_min_history=120,
            factor_regime_fast_ma=20,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_regime_required_signals=3,
        )
        strategy = VolumeTrendFactorStrategy(settings)
        benchmark_data = self._candles("100", "1.2", count=160, final_volume="500", wiggle="0.4")
        market_data = {
            "ETH-USDT": self._candles("100", "2.2", count=160, final_volume="450", wiggle="1.2"),
            "SOL-USDT": self._candles("100", "1.8", count=160, final_volume="350", wiggle="1.0"),
        }
        regime = strategy.regime(market_data, benchmark_data)
        self.assertEqual(strategy._selection_top_n(regime), 2)
        picks = strategy.evaluate(market_data, benchmark_data=benchmark_data)
        self.assertEqual({pick.inst_id for pick in picks}, {"ETH-USDT", "SOL-USDT"})
        self.assertEqual(len(picks), 2)
        self.assertLessEqual(sum(pick.weight for pick in picks), 0.90 + 1e-9)

    def test_dynamic_top_n_stays_at_base_when_regime_is_not_strong_enough(self) -> None:
        settings = Settings(
            factor_top_n=1,
            factor_dynamic_top_n_enabled=True,
            factor_dynamic_top_n=2,
            factor_dynamic_top_n_required_signals=4,
            factor_dynamic_top_n_breadth_threshold=Decimal("0.60"),
            factor_dynamic_top_n_benchmark_momentum=Decimal("0.08"),
            factor_min_history=120,
            factor_regime_fast_ma=20,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_regime_required_signals=3,
        )
        strategy = VolumeTrendFactorStrategy(settings)
        benchmark_data = self._candles("100", "1.2", count=160, final_volume="500", wiggle="0.4")
        market_data = {
            "ETH-USDT": self._candles("100", "2.2", count=160, final_volume="450", wiggle="1.2"),
            "SOL-USDT": self._candles("100", "-0.5", count=160, final_volume="150", wiggle="0.8"),
        }
        regime = strategy.regime(market_data, benchmark_data)
        self.assertEqual(strategy._selection_top_n(regime), 1)
        picks = strategy.evaluate(market_data, benchmark_data=benchmark_data)
        self.assertEqual([pick.inst_id for pick in picks], ["ETH-USDT"])
        self.assertEqual(len(picks), 1)

    def test_evaluate_falls_back_to_benchmark_when_btc_trend_is_on(self) -> None:
        settings = Settings(
            factor_top_n=1,
            factor_min_history=90,
            factor_regime_fast_ma=20,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_regime_required_signals=4,
            factor_regime_breadth_threshold=Decimal("0.80"),
        )
        strategy = VolumeTrendFactorStrategy(settings)
        benchmark_data = self._candles("100", "2", count=120, final_volume="400")
        market_data = {
            "ETH-USDT": self._candles("100", "0.3", count=120, final_volume="250"),
            "DOGE-USDT": self._candles("100", "-0.5", count=120),
        }
        picks = strategy.evaluate(market_data, benchmark_data=benchmark_data)
        self.assertEqual([pick.inst_id for pick in picks], ["BTC-USDT"])
        self.assertAlmostEqual(picks[0].weight, 1.0, places=6)

    def test_vol_target_scales_total_weight_below_one(self) -> None:
        settings = Settings(
            factor_top_n=2,
            factor_min_history=120,
            factor_regime_fast_ma=20,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_target_annual_vol=Decimal("0.12"),
            factor_min_gross_exposure=Decimal("0.10"),
            factor_max_gross_exposure=Decimal("1.00"),
        )
        strategy = VolumeTrendFactorStrategy(settings)
        benchmark_data = self._candles("100", "1", count=160, final_volume="400", wiggle="0.8")
        market_data = {
            "BTC-USDT": benchmark_data,
            "ETH-USDT": self._candles("100", "2.2", count=160, final_volume="350", wiggle="2.2"),
            "SOL-USDT": self._candles("100", "1.8", count=160, final_volume="300", wiggle="2.8"),
        }
        picks = strategy.evaluate(market_data, benchmark_data=benchmark_data)
        self.assertTrue(picks)
        self.assertLess(sum(pick.weight for pick in picks), 1.0)

    def test_market_state_exposure_multiplier_scales_weights(self) -> None:
        settings = Settings(
            factor_top_n=2,
            factor_min_history=120,
            factor_regime_fast_ma=20,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_target_annual_vol=Decimal("1.00"),
            factor_min_gross_exposure=Decimal("0.10"),
            factor_max_gross_exposure=Decimal("1.00"),
        )
        strategy = VolumeTrendFactorStrategy(settings)
        benchmark_data = self._candles("100", "1", count=160, final_volume="400", wiggle="0.8")
        market_data = {
            "BTC-USDT": benchmark_data,
            "ETH-USDT": self._candles("100", "2.2", count=160, final_volume="350", wiggle="2.2"),
            "SOL-USDT": self._candles("100", "1.8", count=160, final_volume="300", wiggle="2.8"),
        }
        base = strategy.evaluate(market_data, benchmark_data=benchmark_data)
        reduced = strategy.evaluate(
            market_data,
            benchmark_data=benchmark_data,
            market_state=self._market_state(exposure_multiplier=0.5),
        )
        self.assertTrue(base)
        self.assertEqual([pick.inst_id for pick in base], [pick.inst_id for pick in reduced])
        self.assertAlmostEqual(sum(pick.weight for pick in reduced), sum(pick.weight for pick in base) * 0.5, places=6)

    def test_resume_ready_requires_confirm_bars(self) -> None:
        settings = Settings(
            factor_min_history=120,
            factor_regime_fast_ma=20,
            factor_regime_slow_ma=60,
            factor_regime_momentum_lookback=30,
            factor_halt_resume_confirm_bars=3,
            factor_halt_resume_required_signals=3,
        )
        strategy = VolumeTrendFactorStrategy(settings)
        benchmark = self._candles("100", "1.5", count=160, final_volume="400", wiggle="0.6")
        market_data = {
            "BTC-USDT": benchmark,
            "ETH-USDT": self._candles("100", "2.0", count=160, final_volume="300", wiggle="1.2"),
        }
        self.assertTrue(strategy.resume_ready(market_data, benchmark))

        weakened = benchmark[:-2] + [
            Candle(
                ts=benchmark[-2].ts,
                open=Decimal("180"),
                high=Decimal("180"),
                low=Decimal("180"),
                close=Decimal("180"),
                volume=Decimal("100"),
                quote_volume=Decimal("18000"),
                confirmed=True,
            ),
            Candle(
                ts=benchmark[-1].ts,
                open=Decimal("160"),
                high=Decimal("160"),
                low=Decimal("160"),
                close=Decimal("160"),
                volume=Decimal("100"),
                quote_volume=Decimal("16000"),
                confirmed=True,
            ),
        ]
        weakened_market = {
            "BTC-USDT": weakened,
            "ETH-USDT": market_data["ETH-USDT"],
        }
        self.assertFalse(strategy.resume_ready(weakened_market, weakened))


class FactorPortfolioBotOrderTest(unittest.TestCase):
    def test_build_orders_generates_sells_before_buys(self) -> None:
        settings = Settings(
            factor_quote_currency="USDT",
            factor_capital_fraction=Decimal("0.90"),
            factor_min_order_quote=Decimal("10"),
            min_cash_reserve_quote=Decimal("10"),
        )
        bot = FactorPortfolioBot(settings)
        bot._rules_cache = {
            "BTC-USDT": InstrumentRules(min_size=Decimal("0.01"), lot_size=Decimal("0.01"), tick_size=Decimal("0.1")),
            "ETH-USDT": InstrumentRules(min_size=Decimal("0.01"), lot_size=Decimal("0.01"), tick_size=Decimal("0.1")),
            "SOL-USDT": InstrumentRules(min_size=Decimal("0.01"), lot_size=Decimal("0.01"), tick_size=Decimal("0.1")),
        }
        picks = [
            FactorCandidate(
                inst_id="ETH-USDT",
                price=Decimal("50"),
                score=3.0,
                weight=0.50,
                momentum_short=0.2,
                momentum_medium=0.4,
                momentum_long=0.8,
                fast_gap=0.1,
                slow_gap=0.2,
                volume_ratio=1.5,
                volatility=0.05,
            ),
            FactorCandidate(
                inst_id="BTC-USDT",
                price=Decimal("100"),
                score=2.0,
                weight=0.40,
                momentum_short=0.1,
                momentum_medium=0.3,
                momentum_long=0.6,
                fast_gap=0.1,
                slow_gap=0.1,
                volume_ratio=1.2,
                volatility=0.05,
            ),
        ]
        balances = {
            "USDT": Decimal("100"),
            "BTC": Decimal("1"),
            "SOL": Decimal("1"),
        }
        ticker_map = {
            "BTC-USDT": SpotTicker("BTC-USDT", Decimal("100"), Decimal("1000000"), Decimal("10000")),
            "ETH-USDT": SpotTicker("ETH-USDT", Decimal("50"), Decimal("1000000"), Decimal("10000")),
            "SOL-USDT": SpotTicker("SOL-USDT", Decimal("25"), Decimal("1000000"), Decimal("10000")),
        }
        total_equity, available_quote, orders = bot._build_orders(picks, balances, ticker_map)
        self.assertEqual(total_equity, Decimal("225"))
        self.assertEqual(available_quote, Decimal("100"))
        self.assertEqual([order.side for order in orders], ["sell", "sell", "buy"])
        self.assertEqual([order.inst_id for order in orders], ["BTC-USDT", "SOL-USDT", "ETH-USDT"])

    def test_build_orders_blocks_new_entries_in_reduce_only_mode(self) -> None:
        settings = Settings(
            factor_quote_currency="USDT",
            factor_capital_fraction=Decimal("0.90"),
            factor_min_order_quote=Decimal("10"),
            min_cash_reserve_quote=Decimal("10"),
        )
        bot = FactorPortfolioBot(settings)
        bot._rules_cache = {
            "BTC-USDT": InstrumentRules(min_size=Decimal("0.01"), lot_size=Decimal("0.01"), tick_size=Decimal("0.1")),
            "ETH-USDT": InstrumentRules(min_size=Decimal("0.01"), lot_size=Decimal("0.01"), tick_size=Decimal("0.1")),
        }
        picks = [
            FactorCandidate(
                inst_id="ETH-USDT",
                price=Decimal("50"),
                score=3.0,
                weight=0.50,
                momentum_short=0.2,
                momentum_medium=0.4,
                momentum_long=0.8,
                fast_gap=0.1,
                slow_gap=0.2,
                volume_ratio=1.5,
                volatility=0.05,
            )
        ]
        balances = {
            "USDT": Decimal("100"),
            "BTC": Decimal("1"),
        }
        ticker_map = {
            "BTC-USDT": SpotTicker("BTC-USDT", Decimal("100"), Decimal("1000000"), Decimal("10000")),
            "ETH-USDT": SpotTicker("ETH-USDT", Decimal("50"), Decimal("1000000"), Decimal("10000")),
        }
        _, _, orders = bot._build_orders(picks, balances, ticker_map, allow_new_entries=False)
        self.assertEqual([order.side for order in orders], ["sell"])
        self.assertEqual([order.inst_id for order in orders], ["BTC-USDT"])


if __name__ == "__main__":
    unittest.main()
