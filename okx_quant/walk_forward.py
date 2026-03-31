from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from okx_quant.backtest import BacktestReport, EquityPoint, FactorBacktester
from okx_quant.config import Settings


@dataclass(frozen=True)
class WalkForwardSplit:
    split_index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    best_params: dict[str, Any]
    train_return: float
    train_max_drawdown: float
    train_sharpe: float
    test_return: float
    test_max_drawdown: float
    test_sharpe: float


@dataclass(frozen=True)
class WalkForwardReport:
    lookback_years: int
    train_days: int
    test_days: int
    step_days: int
    search_profile: str
    config_count: int
    splits: list[WalkForwardSplit]
    out_of_sample_total_return: float
    out_of_sample_max_drawdown: float
    avg_test_return: float
    avg_test_sharpe: float
    universe: list[str]
    report_path: str


@dataclass(frozen=True)
class CurveMetrics:
    total_return: float
    annualized_volatility: float
    sharpe_ratio: float
    max_drawdown: float


class FactorWalkForwardAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("okx_quant.walk_forward")

    def _jsonable(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, tuple):
            return [self._jsonable(item) for item in value]
        if isinstance(value, list):
            return [self._jsonable(item) for item in value]
        if isinstance(value, dict):
            return {key: self._jsonable(item) for key, item in value.items()}
        return value

    def _serialize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return {key: self._jsonable(value) for key, value in params.items()}

    def _build_grid(
        self,
        *,
        modes: tuple[str, ...],
        top_ns: tuple[int, ...],
        turnovers: tuple[Decimal, ...],
        target_vols: tuple[Decimal, ...],
        max_drawdowns: tuple[Decimal, ...],
        tier_sets: tuple[tuple[tuple[Decimal, Decimal], ...], ...],
        stop_pairs: tuple[tuple[Decimal, Decimal], ...],
        dynamic_variants: dict[int, tuple[dict[str, Any], ...]],
        market_state_variants: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        grid: list[dict[str, Any]] = []
        for mode in modes:
            for top_n in top_ns:
                top_n_dynamic_variants = dynamic_variants.get(top_n, ({},))
                for turnover in turnovers:
                    for target_vol in target_vols:
                        for max_dd in max_drawdowns:
                            for tiers in tier_sets:
                                for stop_loss, trailing in stop_pairs:
                                    for dynamic_params in top_n_dynamic_variants:
                                        for market_state_params in market_state_variants:
                                            candidate = {
                                                "factor_rebalance_mode": mode,
                                                "factor_top_n": top_n,
                                                "factor_hold_buffer": 1 if top_n > 1 else 0,
                                                "factor_max_turnover_per_rebalance": turnover,
                                                "factor_target_annual_vol": target_vol,
                                                "factor_max_drawdown_pct": max_dd,
                                                "factor_drawdown_scale_tiers": tiers,
                                                "factor_stop_loss_pct": stop_loss,
                                                "factor_trailing_stop_pct": trailing,
                                                "factor_halt_cooldown_days": 21,
                                                "factor_halt_resume_confirm_bars": 5,
                                            }
                                            candidate.update(dynamic_params)
                                            candidate.update(market_state_params)
                                            grid.append(candidate)
        return grid

    def _dynamic_top_n_variants(self, profile: str) -> dict[int, tuple[dict[str, Any], ...]]:
        static_two = {
            "factor_dynamic_top_n_enabled": False,
            "factor_dynamic_top_n": 2,
            "factor_dynamic_top_n_required_signals": 4,
            "factor_dynamic_top_n_breadth_threshold": Decimal("0.55"),
            "factor_dynamic_top_n_benchmark_momentum": Decimal("0.08"),
        }
        if profile == "full":
            enabled_variants = (
                {
                    "factor_dynamic_top_n_enabled": True,
                    "factor_dynamic_top_n": 2,
                    "factor_dynamic_top_n_required_signals": 3,
                    "factor_dynamic_top_n_breadth_threshold": Decimal("0.45"),
                    "factor_dynamic_top_n_benchmark_momentum": Decimal("0.05"),
                },
                {
                    "factor_dynamic_top_n_enabled": True,
                    "factor_dynamic_top_n": 2,
                    "factor_dynamic_top_n_required_signals": 4,
                    "factor_dynamic_top_n_breadth_threshold": Decimal("0.55"),
                    "factor_dynamic_top_n_benchmark_momentum": Decimal("0.08"),
                },
                {
                    "factor_dynamic_top_n_enabled": True,
                    "factor_dynamic_top_n": 2,
                    "factor_dynamic_top_n_required_signals": 4,
                    "factor_dynamic_top_n_breadth_threshold": Decimal("0.65"),
                    "factor_dynamic_top_n_benchmark_momentum": Decimal("0.10"),
                },
            )
        else:
            enabled_variants = (
                {
                    "factor_dynamic_top_n_enabled": True,
                    "factor_dynamic_top_n": 2,
                    "factor_dynamic_top_n_required_signals": 3,
                    "factor_dynamic_top_n_breadth_threshold": Decimal("0.45"),
                    "factor_dynamic_top_n_benchmark_momentum": Decimal("0.05"),
                },
                {
                    "factor_dynamic_top_n_enabled": True,
                    "factor_dynamic_top_n": 2,
                    "factor_dynamic_top_n_required_signals": 4,
                    "factor_dynamic_top_n_breadth_threshold": Decimal("0.55"),
                    "factor_dynamic_top_n_benchmark_momentum": Decimal("0.08"),
                },
            )
        return {
            1: enabled_variants,
            2: (static_two,),
        }

    def _market_state_variants(self, profile: str) -> tuple[dict[str, Any], ...]:
        # Only search parameters that can be replayed from historical candles.
        baseline_disabled = {
            "factor_market_state_enabled": False,
        }
        lenient = {
            "factor_market_state_enabled": True,
            "factor_market_state_min_breadth": Decimal("0.15"),
            "factor_market_state_min_benchmark_momentum": Decimal("-0.02"),
            "factor_market_state_max_momentum_dispersion": Decimal("0.45"),
            "factor_market_state_min_volume_ratio": Decimal("0.70"),
            "factor_market_state_min_exposure": Decimal("0.50"),
            "factor_market_state_entry_gate": Decimal("0.45"),
        }
        balanced = {
            "factor_market_state_enabled": True,
            "factor_market_state_min_breadth": Decimal("0.25"),
            "factor_market_state_min_benchmark_momentum": Decimal("0.00"),
            "factor_market_state_max_momentum_dispersion": Decimal("0.35"),
            "factor_market_state_min_volume_ratio": Decimal("0.80"),
            "factor_market_state_min_exposure": Decimal("0.35"),
            "factor_market_state_entry_gate": Decimal("0.60"),
        }
        recovery = {
            "factor_market_state_enabled": True,
            "factor_market_state_min_breadth": Decimal("0.20"),
            "factor_market_state_min_benchmark_momentum": Decimal("-0.01"),
            "factor_market_state_max_momentum_dispersion": Decimal("0.30"),
            "factor_market_state_min_volume_ratio": Decimal("0.85"),
            "factor_market_state_min_exposure": Decimal("0.50"),
            "factor_market_state_entry_gate": Decimal("0.55"),
        }
        strict = {
            "factor_market_state_enabled": True,
            "factor_market_state_min_breadth": Decimal("0.35"),
            "factor_market_state_min_benchmark_momentum": Decimal("0.03"),
            "factor_market_state_max_momentum_dispersion": Decimal("0.25"),
            "factor_market_state_min_volume_ratio": Decimal("0.90"),
            "factor_market_state_min_exposure": Decimal("0.25"),
            "factor_market_state_entry_gate": Decimal("0.70"),
        }
        if profile == "quick":
            return (baseline_disabled, balanced, strict)
        if profile == "full":
            return (baseline_disabled, lenient, recovery, balanced, strict)
        return (baseline_disabled, lenient, balanced, strict)

    def _parameter_grid(self, profile: str = "default") -> list[dict[str, Any]]:
        if profile not in {"quick", "default", "full"}:
            raise ValueError(f"Unsupported walk-forward profile: {profile}")
        aggressive_tiers = (
            (Decimal("0.06"), Decimal("0.75")),
            (Decimal("0.12"), Decimal("0.50")),
            (Decimal("0.18"), Decimal("0.30")),
            (Decimal("0.24"), Decimal("0.10")),
        )
        balanced_tiers = (
            (Decimal("0.08"), Decimal("0.85")),
            (Decimal("0.15"), Decimal("0.60")),
            (Decimal("0.22"), Decimal("0.35")),
            (Decimal("0.30"), Decimal("0.15")),
        )
        dynamic_variants = self._dynamic_top_n_variants(profile)
        market_state_variants = self._market_state_variants(profile)
        if profile == "quick":
            return self._build_grid(
                modes=("weekly", "monthly"),
                top_ns=(1, 2),
                turnovers=(Decimal("0.35"), Decimal("0.50")),
                target_vols=(Decimal("0.45"), Decimal("0.55")),
                max_drawdowns=(Decimal("0.30"),),
                tier_sets=(balanced_tiers,),
                stop_pairs=((Decimal("0.08"), Decimal("0.12")),),
                dynamic_variants=dynamic_variants,
                market_state_variants=market_state_variants,
            )
        if profile == "full":
            return self._build_grid(
                modes=("weekly", "monthly"),
                top_ns=(1, 2),
                turnovers=(Decimal("0.35"), Decimal("0.50")),
                target_vols=(Decimal("0.35"), Decimal("0.45"), Decimal("0.55")),
                max_drawdowns=(Decimal("0.30"), Decimal("0.32")),
                tier_sets=(aggressive_tiers, balanced_tiers),
                stop_pairs=(
                    (Decimal("0.00"), Decimal("0.00")),
                    (Decimal("0.08"), Decimal("0.12")),
                ),
                dynamic_variants=dynamic_variants,
                market_state_variants=market_state_variants,
            )
        return self._build_grid(
            modes=("weekly", "monthly"),
            top_ns=(1, 2),
            turnovers=(Decimal("0.35"), Decimal("0.50")),
            target_vols=(Decimal("0.45"), Decimal("0.55")),
            max_drawdowns=(Decimal("0.30"),),
            tier_sets=(aggressive_tiers, balanced_tiers),
            stop_pairs=(
                (Decimal("0.00"), Decimal("0.00")),
                (Decimal("0.08"), Decimal("0.12")),
            ),
            dynamic_variants=dynamic_variants,
            market_state_variants=market_state_variants,
        )

    def _curve_metrics(self, equity_curve: list[EquityPoint]) -> CurveMetrics:
        if len(equity_curve) < 2:
            return CurveMetrics(0.0, 0.0, 0.0, 0.0)

        returns: list[float] = []
        peak = equity_curve[0].equity
        max_drawdown = Decimal("0")
        for index, point in enumerate(equity_curve):
            peak = max(peak, point.equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - point.equity) / peak)
            if index == 0:
                continue
            previous = float(equity_curve[index - 1].equity)
            current = float(point.equity)
            if previous > 0:
                returns.append(current / previous - 1.0)

        returns_stdev = pstdev(returns) if len(returns) > 1 else 0.0
        annualized_volatility = returns_stdev * (365.0**0.5) if returns_stdev > 0 else 0.0
        sharpe_ratio = (mean(returns) / returns_stdev * (365.0**0.5)) if returns_stdev > 0 else 0.0
        total_return = float(equity_curve[-1].equity / equity_curve[0].equity - Decimal("1"))
        return CurveMetrics(
            total_return=total_return,
            annualized_volatility=annualized_volatility,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=float(max_drawdown),
        )

    def _objective(self, report: BacktestReport, equity_curve: list[EquityPoint] | None = None) -> float:
        # Train on the whole window, but overweight the latter half so the search
        # prefers configurations that keep working instead of peaking early.
        capped_return = min(report.total_return, 1.0)
        if not equity_curve or len(equity_curve) < 20:
            return capped_return - 1.10 * report.max_drawdown + 0.15 * report.sharpe_ratio

        tail_start = max(0, len(equity_curve) // 2 - 1)
        tail_metrics = self._curve_metrics(equity_curve[tail_start:])
        capped_tail_return = min(tail_metrics.total_return, 0.75)
        fade_penalty = max(0.0, capped_return - max(tail_metrics.total_return, 0.0))
        turnover_penalty = 0.015 * max(0.0, report.turnover_ratio - 12.0)
        volatility_penalty = 0.35 * max(0.0, report.annualized_volatility - 0.65)
        return (
            0.70 * capped_return
            + 0.95 * capped_tail_return
            - 0.90 * report.max_drawdown
            - 1.35 * tail_metrics.max_drawdown
            + 0.08 * report.sharpe_ratio
            + 0.25 * tail_metrics.sharpe_ratio
            - 0.40 * fade_penalty
            - turnover_penalty
            - volatility_penalty
        )

    def _stitch_equity(
        self,
        initial_capital: Decimal,
        equity_curves: list[list[EquityPoint]],
    ) -> tuple[float, float]:
        if not equity_curves:
            return 0.0, 0.0

        capital = initial_capital
        stitched: list[Decimal] = []
        for curve in equity_curves:
            if not curve:
                continue
            base = curve[0].equity
            for point in curve:
                scaled = capital * (point.equity / base)
                stitched.append(scaled)
            capital = stitched[-1]

        if not stitched:
            return 0.0, 0.0

        peak = stitched[0]
        max_drawdown = Decimal("0")
        for equity in stitched:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)
        total_return = float(stitched[-1] / initial_capital - Decimal("1"))
        return total_return, float(max_drawdown)

    def run(
        self,
        *,
        lookback_years: int = 4,
        train_days: int = 365,
        test_days: int = 90,
        step_days: int = 90,
        search_profile: str = "default",
        max_configs: int | None = None,
    ) -> WalkForwardReport:
        base_backtester = FactorBacktester(self.settings)
        universe, history = base_backtester.load_history_for_years([lookback_years])
        all_ts = sorted({candle.ts for candles in history.values() for candle in candles if candle.confirmed})
        if len(all_ts) < self.settings.factor_min_history + train_days + test_days:
            raise RuntimeError("Not enough history for walk-forward analysis")

        splits: list[WalkForwardSplit] = []
        test_curves: list[list[EquityPoint]] = []
        split_index = 1
        start_index = self.settings.factor_min_history
        grid = self._parameter_grid(search_profile)
        if max_configs is not None:
            grid = grid[:max_configs]
        if not grid:
            raise RuntimeError("Walk-forward parameter grid is empty")
        self.logger.info(
            "Walk-forward loaded universe=%s bars=%s profile=%s grid=%s",
            universe,
            len(all_ts),
            search_profile,
            len(grid),
        )

        while start_index + train_days + test_days <= len(all_ts):
            train_start = all_ts[start_index]
            train_end = all_ts[start_index + train_days - 1]
            test_start = all_ts[start_index + train_days]
            test_end = all_ts[start_index + train_days + test_days - 1]
            self.logger.info(
                "Walk-forward split=%s train=%s..%s test=%s..%s",
                split_index,
                train_start.date().isoformat(),
                train_end.date().isoformat(),
                test_start.date().isoformat(),
                test_end.date().isoformat(),
            )

            best_score: float | None = None
            best_params: dict[str, Any] | None = None
            best_train_report: BacktestReport | None = None
            best_test_report: BacktestReport | None = None
            best_test_curve: list[EquityPoint] | None = None

            for params in grid:
                candidate_settings = Settings(
                    factor_universe=tuple(universe),
                    factor_min_24h_quote_volume=Decimal("0"),
                    **params,
                )
                candidate_backtester = FactorBacktester(candidate_settings)
                train_report, train_curve, _ = candidate_backtester.simulate_range(
                    history,
                    universe,
                    train_start,
                    train_end,
                    label=f"wf_train_{split_index}",
                )
                score = self._objective(train_report, train_curve)
                if best_score is not None and score <= best_score:
                    continue

                test_report, test_curve, _ = candidate_backtester.simulate_range(
                    history,
                    universe,
                    test_start,
                    test_end,
                    label=f"wf_test_{split_index}",
                )
                best_score = score
                best_params = params
                best_train_report = train_report
                best_test_report = test_report
                best_test_curve = test_curve

            if best_params is None or best_train_report is None or best_test_report is None or best_test_curve is None:
                raise RuntimeError("Walk-forward optimization failed to produce a split result")

            self.logger.info(
                "Walk-forward split=%s best train_return=%.2f%% train_mdd=%.2f%% test_return=%.2f%% test_mdd=%.2f%% params=%s",
                split_index,
                best_train_report.total_return * 100.0,
                best_train_report.max_drawdown * 100.0,
                best_test_report.total_return * 100.0,
                best_test_report.max_drawdown * 100.0,
                self._serialize_params(best_params),
            )
            test_curves.append(best_test_curve)
            splits.append(
                WalkForwardSplit(
                    split_index=split_index,
                    train_start=train_start.date().isoformat(),
                    train_end=train_end.date().isoformat(),
                    test_start=test_start.date().isoformat(),
                    test_end=test_end.date().isoformat(),
                    best_params=self._serialize_params(best_params),
                    train_return=best_train_report.total_return,
                    train_max_drawdown=best_train_report.max_drawdown,
                    train_sharpe=best_train_report.sharpe_ratio,
                    test_return=best_test_report.total_return,
                    test_max_drawdown=best_test_report.max_drawdown,
                    test_sharpe=best_test_report.sharpe_ratio,
                )
            )
            split_index += 1
            start_index += step_days

        oos_return, oos_max_drawdown = self._stitch_equity(self.settings.factor_backtest_initial_capital, test_curves)
        output_dir = Path(self.settings.factor_walk_forward_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        report_path = output_dir / f"factor_walk_forward_{timestamp}.json"

        report = WalkForwardReport(
            lookback_years=lookback_years,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            search_profile=search_profile,
            config_count=len(grid),
            splits=splits,
            out_of_sample_total_return=oos_return,
            out_of_sample_max_drawdown=oos_max_drawdown,
            avg_test_return=mean(split.test_return for split in splits) if splits else 0.0,
            avg_test_sharpe=mean(split.test_sharpe for split in splits) if splits else 0.0,
            universe=universe,
            report_path=str(report_path),
        )
        report_path.write_text(json.dumps(asdict(report), indent=2, ensure_ascii=True), encoding="utf-8")
        return report
