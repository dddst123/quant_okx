from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import log, sqrt
from statistics import fmean, pstdev

from okx_quant.config import Settings
from okx_quant.market_state import MarketStateSnapshot
from okx_quant.models import Candle
from okx_quant.timeframe import bars_per_year


@dataclass(frozen=True)
class FactorCandidate:
    inst_id: str
    price: Decimal
    score: float
    weight: float
    momentum_short: float
    momentum_medium: float
    momentum_long: float
    fast_gap: float
    slow_gap: float
    volume_ratio: float
    volatility: float


@dataclass(frozen=True)
class RegimeSnapshot:
    risk_on: bool
    benchmark_trend_on: bool
    positive_signals: int
    benchmark_price: Decimal
    benchmark_fast_ma: float
    benchmark_slow_ma: float
    benchmark_momentum: float
    breadth: float
    reason: str


class VolumeTrendFactorStrategy:
    """Long-only crypto trend factor with regime gating and turnover-aware persistence."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _close_series(self, candles: list[Candle]) -> list[float]:
        return [float(candle.close) for candle in candles]

    def _volume_series(self, candles: list[Candle]) -> list[float]:
        return [float(candle.quote_volume or candle.volume) for candle in candles]

    def _return(self, values: list[float], lookback: int) -> float:
        return values[-1] / values[-(lookback + 1)] - 1.0

    def _sma(self, values: list[float], window: int) -> float:
        return fmean(values[-window:])

    def _volatility(self, values: list[float], window: int) -> float:
        sample = values[-(window + 1) :]
        log_returns = [log(sample[idx] / sample[idx - 1]) for idx in range(1, len(sample))]
        return max(pstdev(log_returns), 1e-6)

    def _confirmed(self, candles: list[Candle]) -> list[Candle]:
        return [candle for candle in candles if candle.confirmed]

    def _log_returns(self, candles: list[Candle], window: int) -> list[float]:
        confirmed = self._confirmed(candles)
        closes = self._close_series(confirmed)[-(window + 1) :]
        return [log(closes[idx] / closes[idx - 1]) for idx in range(1, len(closes))]

    def _trend_metrics(self, candles: list[Candle]) -> dict[str, float]:
        confirmed = self._confirmed(candles)
        if len(confirmed) < self.settings.factor_min_history:
            raise ValueError("insufficient candles for factor metrics")
        closes = self._close_series(confirmed)
        volumes = self._volume_series(confirmed)
        volatility = self._volatility(closes, self.settings.factor_volatility_lookback)
        momentum_short = self._return(closes, self.settings.factor_short_lookback)
        momentum_medium = self._return(closes, self.settings.factor_medium_lookback)
        momentum_long = self._return(closes, self.settings.factor_long_lookback)
        fast_ma = self._sma(closes, self.settings.factor_fast_ma)
        slow_ma = self._sma(closes, self.settings.factor_slow_ma)
        fast_gap = closes[-1] / fast_ma - 1.0
        slow_gap = closes[-1] / slow_ma - 1.0
        volume_ratio = volumes[-1] / max(self._sma(volumes[:-1], self.settings.factor_volume_lookback), 1e-6)
        return {
            "price": closes[-1],
            "volatility": volatility,
            "momentum_short": momentum_short,
            "momentum_medium": momentum_medium,
            "momentum_long": momentum_long,
            "fast_gap": fast_gap,
            "slow_gap": slow_gap,
            "volume_ratio": volume_ratio,
        }

    def _required_benchmark_history(self) -> int:
        return max(
            self.settings.factor_min_history,
            self.settings.factor_regime_slow_ma + 1,
            self.settings.factor_regime_momentum_lookback + 1,
        )

    def _selection_top_n(self, regime: RegimeSnapshot | None) -> int:
        top_n = self.settings.factor_top_n
        if regime is None or not self.settings.factor_dynamic_top_n_enabled:
            return top_n
        if not regime.benchmark_trend_on:
            return top_n
        if regime.positive_signals < self.settings.factor_dynamic_top_n_required_signals:
            return top_n
        if regime.breadth < float(self.settings.factor_dynamic_top_n_breadth_threshold):
            return top_n
        if regime.benchmark_momentum < float(self.settings.factor_dynamic_top_n_benchmark_momentum):
            return top_n
        return max(top_n, self.settings.factor_dynamic_top_n)

    def regime(self, market_data: dict[str, list[Candle]], benchmark_data: list[Candle]) -> RegimeSnapshot:
        benchmark = self._confirmed(benchmark_data)
        if len(benchmark) < self._required_benchmark_history():
            return RegimeSnapshot(False, False, 0, Decimal("0"), 0.0, 0.0, 0.0, 0.0, "insufficient benchmark history")

        benchmark_closes = self._close_series(benchmark)
        benchmark_price = benchmark[-1].close
        benchmark_fast_ma = self._sma(benchmark_closes, self.settings.factor_regime_fast_ma)
        benchmark_slow_ma = self._sma(benchmark_closes, self.settings.factor_regime_slow_ma)
        benchmark_momentum = self._return(benchmark_closes, self.settings.factor_regime_momentum_lookback)

        passing_assets = 0
        total_assets = 0
        for inst_id, candles in market_data.items():
            if inst_id == self.settings.factor_benchmark_inst_id:
                continue
            confirmed = self._confirmed(candles)
            if len(confirmed) < self.settings.factor_min_history:
                continue
            total_assets += 1
            metrics = self._trend_metrics(confirmed)
            if metrics["momentum_medium"] > 0 and metrics["slow_gap"] > 0:
                passing_assets += 1
        breadth = passing_assets / total_assets if total_assets else 0.0

        signals = [
            benchmark_price > Decimal(str(benchmark_fast_ma)),
            benchmark_fast_ma > benchmark_slow_ma,
            benchmark_momentum > 0,
            breadth >= float(self.settings.factor_regime_breadth_threshold),
        ]
        positive_signals = sum(1 for passed in signals if passed)
        benchmark_trend_on = all(signals[:3])
        risk_on = positive_signals >= self.settings.factor_regime_required_signals
        reason = (
            f"signals={positive_signals}/4 price_above_fast={signals[0]} fast_above_slow={signals[1]} "
            f"benchmark_momentum={benchmark_momentum:.2%} breadth={breadth:.2%}"
        )
        return RegimeSnapshot(
            risk_on=risk_on,
            benchmark_trend_on=benchmark_trend_on,
            positive_signals=positive_signals,
            benchmark_price=benchmark_price,
            benchmark_fast_ma=benchmark_fast_ma,
            benchmark_slow_ma=benchmark_slow_ma,
            benchmark_momentum=benchmark_momentum,
            breadth=breadth,
            reason=reason,
        )

    def _cap_weights(self, raw_weights: list[tuple[FactorCandidate, float]]) -> list[FactorCandidate]:
        if not raw_weights:
            return []

        # Keep single-name fallback fully deployable, but enforce the configured cap
        # once the strategy is rotating across multiple names.
        max_weight = 1.0 if len(raw_weights) == 1 else float(self.settings.factor_max_asset_weight)
        pending = list(raw_weights)
        final_weights: dict[str, float] = {}
        remaining_weight = 1.0

        while pending and remaining_weight > 0:
            total_raw = sum(weight for _, weight in pending)
            if total_raw <= 0:
                break

            overflow: list[tuple[FactorCandidate, float]] = []
            for candidate, raw_weight in pending:
                scaled = remaining_weight * (raw_weight / total_raw)
                if scaled > max_weight:
                    final_weights[candidate.inst_id] = max_weight
                    remaining_weight -= max_weight
                else:
                    overflow.append((candidate, raw_weight))

            if len(overflow) == len(pending):
                total_raw = sum(weight for _, weight in overflow)
                for candidate, raw_weight in overflow:
                    final_weights[candidate.inst_id] = remaining_weight * (raw_weight / total_raw)
                break

            pending = overflow

        ranked: list[FactorCandidate] = []
        for candidate, _ in raw_weights:
            ranked.append(
                FactorCandidate(
                    inst_id=candidate.inst_id,
                    price=candidate.price,
                    score=candidate.score,
                    weight=final_weights.get(candidate.inst_id, 0.0),
                    momentum_short=candidate.momentum_short,
                    momentum_medium=candidate.momentum_medium,
                    momentum_long=candidate.momentum_long,
                    fast_gap=candidate.fast_gap,
                    slow_gap=candidate.slow_gap,
                    volume_ratio=candidate.volume_ratio,
                    volatility=candidate.volatility,
                )
            )
        return ranked

    def _benchmark_candidate(self, benchmark_data: list[Candle]) -> FactorCandidate | None:
        confirmed = self._confirmed(benchmark_data)
        if len(confirmed) < self._required_benchmark_history():
            return None
        metrics = self._trend_metrics(confirmed)
        risk_adjusted_momentum = (
            0.40 * (metrics["momentum_medium"] / metrics["volatility"])
            + 0.60 * (metrics["momentum_long"] / metrics["volatility"])
        )
        trend_strength = 0.35 * metrics["fast_gap"] + 0.65 * metrics["slow_gap"]
        volume_boost = max(0.0, min(metrics["volume_ratio"] - 1.0, 2.0))
        score = 0.65 * risk_adjusted_momentum + 0.25 * trend_strength + 0.10 * volume_boost
        if score <= 0:
            return None
        return FactorCandidate(
            inst_id=self.settings.factor_benchmark_inst_id,
            price=confirmed[-1].close,
            score=score,
            weight=0.0,
            momentum_short=metrics["momentum_short"],
            momentum_medium=metrics["momentum_medium"],
            momentum_long=metrics["momentum_long"],
            fast_gap=metrics["fast_gap"],
            slow_gap=metrics["slow_gap"],
            volume_ratio=metrics["volume_ratio"],
            volatility=metrics["volatility"],
        )

    def _gross_exposure_multiplier(
        self,
        selected: list[FactorCandidate],
        market_data: dict[str, list[Candle]],
    ) -> float:
        if self.settings.factor_target_annual_vol <= 0 or not selected:
            return 1.0

        per_bar_target = float(self.settings.factor_target_annual_vol) / sqrt(bars_per_year(self.settings.factor_bar))
        if per_bar_target <= 0:
            return 1.0

        window = self.settings.factor_volatility_lookback
        weights = [candidate.weight for candidate in selected]
        returns_matrix = [self._log_returns(market_data[candidate.inst_id], window) for candidate in selected]
        length = min((len(series) for series in returns_matrix), default=0)
        if length <= 1:
            return 1.0
        returns_matrix = [series[-length:] for series in returns_matrix]
        means = [fmean(series) for series in returns_matrix]

        portfolio_variance = 0.0
        for i, series_i in enumerate(returns_matrix):
            for j, series_j in enumerate(returns_matrix):
                covariance = sum(
                    (series_i[idx] - means[i]) * (series_j[idx] - means[j])
                    for idx in range(length)
                ) / length
                portfolio_variance += weights[i] * weights[j] * covariance

        daily_vol = sqrt(max(portfolio_variance, 1e-10))
        exposure = per_bar_target / daily_vol
        exposure = max(float(self.settings.factor_min_gross_exposure), exposure)
        exposure = min(float(self.settings.factor_max_gross_exposure), exposure)
        return exposure

    def resume_ready(self, market_data: dict[str, list[Candle]], benchmark_data: list[Candle]) -> bool:
        confirm_bars = self.settings.factor_halt_resume_confirm_bars
        benchmark = self._confirmed(benchmark_data)
        if len(benchmark) < self._required_benchmark_history() + confirm_bars - 1:
            return False

        for offset in range(confirm_bars):
            end = len(benchmark) - offset
            benchmark_subset = benchmark[:end]
            sliced_market_data: dict[str, list[Candle]] = {}
            cutoff_ts = benchmark_subset[-1].ts
            for inst_id, candles in market_data.items():
                sliced = [candle for candle in self._confirmed(candles) if candle.ts <= cutoff_ts]
                if sliced:
                    sliced_market_data[inst_id] = sliced
            regime = self.regime(sliced_market_data, benchmark_subset)
            if not regime.benchmark_trend_on:
                return False
            if regime.positive_signals < self.settings.factor_halt_resume_required_signals:
                return False
        return True

    def evaluate(
        self,
        market_data: dict[str, list[Candle]],
        benchmark_data: list[Candle] | None = None,
        current_positions: set[str] | None = None,
        market_state: MarketStateSnapshot | None = None,
    ) -> list[FactorCandidate]:
        if not market_data:
            return []

        use_regime = benchmark_data is not None
        benchmark_candles = benchmark_data or []
        benchmark_metrics = None
        regime: RegimeSnapshot | None = None
        if use_regime and benchmark_candles:
            confirmed_benchmark = self._confirmed(benchmark_candles)
            if len(confirmed_benchmark) >= self._required_benchmark_history():
                regime = self.regime(market_data, benchmark_candles)
                if not regime.risk_on:
                    if self.settings.factor_benchmark_fallback and regime.benchmark_trend_on:
                        benchmark_candidate = self._benchmark_candidate(benchmark_candles)
                        if benchmark_candidate is not None:
                            return self._cap_weights([(benchmark_candidate, 1.0)])
                    return []
                benchmark_metrics = self._trend_metrics(confirmed_benchmark)
            else:
                return []

        benchmark_medium = benchmark_metrics["momentum_medium"] if benchmark_metrics else 0.0
        benchmark_long = benchmark_metrics["momentum_long"] if benchmark_metrics else 0.0
        current_positions = current_positions or set()
        candidates: list[FactorCandidate] = []

        for inst_id, candles in market_data.items():
            confirmed = self._confirmed(candles)
            if len(confirmed) < self.settings.factor_min_history:
                continue

            metrics = self._trend_metrics(confirmed)
            is_benchmark = inst_id == self.settings.factor_benchmark_inst_id
            relative_strength = 0.60 * (metrics["momentum_medium"] - benchmark_medium) + 0.40 * (
                metrics["momentum_long"] - benchmark_long
            )
            if metrics["momentum_medium"] <= 0 or metrics["momentum_long"] <= 0 or metrics["slow_gap"] <= 0:
                continue
            if not is_benchmark and relative_strength <= 0:
                continue

            risk_adjusted_momentum = (
                0.30 * (metrics["momentum_short"] / metrics["volatility"])
                + 0.40 * (metrics["momentum_medium"] / metrics["volatility"])
                + 0.30 * (metrics["momentum_long"] / metrics["volatility"])
            )
            trend_strength = 0.35 * metrics["fast_gap"] + 0.65 * metrics["slow_gap"]
            volume_boost = max(0.0, min(metrics["volume_ratio"] - 1.0, 2.0))
            relative_component = 0.0 if is_benchmark else relative_strength / max(metrics["volatility"], 1e-6)
            score = (
                0.45 * risk_adjusted_momentum
                + 0.25 * trend_strength
                + 0.20 * relative_component
                + 0.10 * volume_boost
            )
            if is_benchmark:
                score = 0.60 * risk_adjusted_momentum + 0.30 * trend_strength + 0.10 * volume_boost
            if score <= 0:
                continue

            candidates.append(
                FactorCandidate(
                    inst_id=inst_id,
                    price=confirmed[-1].close,
                    score=score,
                    weight=0.0,
                    momentum_short=metrics["momentum_short"],
                    momentum_medium=metrics["momentum_medium"],
                    momentum_long=metrics["momentum_long"],
                    fast_gap=metrics["fast_gap"],
                    slow_gap=metrics["slow_gap"],
                    volume_ratio=metrics["volume_ratio"],
                    volatility=metrics["volatility"],
                )
            )

        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            return []

        target_top_n = self._selection_top_n(regime)
        selected_ids = {candidate.inst_id for candidate in candidates[:target_top_n]}
        if self.settings.factor_hold_buffer > 0 and current_positions:
            buffer_limit = min(len(candidates), target_top_n + self.settings.factor_hold_buffer)
            for candidate in candidates[target_top_n:buffer_limit]:
                if candidate.inst_id in current_positions:
                    selected_ids.add(candidate.inst_id)

        selected = [candidate for candidate in candidates if candidate.inst_id in selected_ids]
        raw_weights = [(candidate, candidate.score / max(candidate.volatility, 1e-6)) for candidate in selected]
        capped = self._cap_weights(raw_weights)
        if not use_regime or not capped:
            return capped

        exposure = self._gross_exposure_multiplier(capped, market_data)
        if market_state is not None:
            exposure *= market_state.exposure_multiplier
            exposure = max(0.0, min(1.0, exposure))
        if exposure >= 0.999:
            return capped
        return [
            FactorCandidate(
                inst_id=candidate.inst_id,
                price=candidate.price,
                score=candidate.score,
                weight=candidate.weight * exposure,
                momentum_short=candidate.momentum_short,
                momentum_medium=candidate.momentum_medium,
                momentum_long=candidate.momentum_long,
                fast_gap=candidate.fast_gap,
                slow_gap=candidate.slow_gap,
                volume_ratio=candidate.volume_ratio,
                volatility=candidate.volatility,
            )
            for candidate in capped
        ]
