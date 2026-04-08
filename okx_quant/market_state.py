from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from math import log
from statistics import fmean, median, pstdev

from okx_quant.client import OkxRestClient
from okx_quant.config import Settings
from okx_quant.models import Candle


@dataclass(frozen=True)
class PublicMarketStateSnapshot:
    benchmark_inst_id: str
    benchmark_swap_inst_id: str
    spread_bps: float | None
    bid_depth_quote: Decimal | None
    ask_depth_quote: Decimal | None
    funding_rate: float | None
    open_interest_usd: Decimal | None
    ts: datetime
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark_inst_id": self.benchmark_inst_id,
            "benchmark_swap_inst_id": self.benchmark_swap_inst_id,
            "spread_bps": self.spread_bps,
            "bid_depth_quote": None if self.bid_depth_quote is None else str(self.bid_depth_quote),
            "ask_depth_quote": None if self.ask_depth_quote is None else str(self.ask_depth_quote),
            "funding_rate": self.funding_rate,
            "open_interest_usd": None if self.open_interest_usd is None else str(self.open_interest_usd),
            "ts": self.ts.isoformat(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class MarketStateSnapshot:
    benchmark_inst_id: str
    benchmark_swap_inst_id: str
    ts: datetime
    breadth: float
    benchmark_momentum: float
    benchmark_slow_gap: float
    benchmark_volatility: float
    momentum_dispersion: float
    median_volume_ratio: float
    spread_bps: float | None
    bid_depth_quote: Decimal | None
    ask_depth_quote: Decimal | None
    funding_rate: float | None
    open_interest_usd: Decimal | None
    risk_score: float
    exposure_multiplier: float
    entries_allowed: bool
    reduce_only: bool
    reason: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark_inst_id": self.benchmark_inst_id,
            "benchmark_swap_inst_id": self.benchmark_swap_inst_id,
            "ts": self.ts.isoformat(),
            "breadth": self.breadth,
            "benchmark_momentum": self.benchmark_momentum,
            "benchmark_slow_gap": self.benchmark_slow_gap,
            "benchmark_volatility": self.benchmark_volatility,
            "momentum_dispersion": self.momentum_dispersion,
            "median_volume_ratio": self.median_volume_ratio,
            "spread_bps": self.spread_bps,
            "bid_depth_quote": None if self.bid_depth_quote is None else str(self.bid_depth_quote),
            "ask_depth_quote": None if self.ask_depth_quote is None else str(self.ask_depth_quote),
            "funding_rate": self.funding_rate,
            "open_interest_usd": None if self.open_interest_usd is None else str(self.open_interest_usd),
            "risk_score": self.risk_score,
            "exposure_multiplier": self.exposure_multiplier,
            "entries_allowed": self.entries_allowed,
            "reduce_only": self.reduce_only,
            "reason": self.reason,
            "notes": list(self.notes),
        }


class MarketStateEngine:
    """Builds a market-state snapshot for gating and de-risking, not direct alpha scoring."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("okx_quant.market_state")

    def _confirmed(self, candles: list[Candle]) -> list[Candle]:
        return [candle for candle in candles if candle.confirmed]

    def _close_series(self, candles: list[Candle]) -> list[float]:
        return [float(candle.close) for candle in candles]

    def _volume_series(self, candles: list[Candle]) -> list[float]:
        return [float(candle.quote_volume or (candle.volume * candle.close)) for candle in candles]

    def _return(self, values: list[float], lookback: int) -> float:
        return values[-1] / values[-(lookback + 1)] - 1.0

    def _sma(self, values: list[float], window: int) -> float:
        return fmean(values[-window:])

    def _volatility(self, values: list[float], window: int) -> float:
        sample = values[-(window + 1) :]
        log_returns = [log(sample[index] / sample[index - 1]) for index in range(1, len(sample))]
        return max(pstdev(log_returns), 1e-6)

    def _required_benchmark_history(self) -> int:
        return max(
            self.settings.factor_min_history,
            self.settings.factor_regime_slow_ma + 1,
            self.settings.factor_regime_momentum_lookback + 1,
            self.settings.factor_volume_lookback + 1,
            self.settings.factor_volatility_lookback + 1,
        )

    def _benchmark_swap_inst_id(self) -> str:
        if self.settings.factor_market_state_swap_inst_id:
            return self.settings.factor_market_state_swap_inst_id
        base, quote = self.settings.factor_benchmark_inst_id.split("-", 1)
        return f"{base}-{quote}-SWAP"

    def collect_public_state(self, client: OkxRestClient) -> PublicMarketStateSnapshot:
        benchmark_inst_id = self.settings.factor_benchmark_inst_id
        benchmark_swap_inst_id = self._benchmark_swap_inst_id()
        now = datetime.now(UTC)
        notes: list[str] = []
        spread_bps: float | None = None
        bid_depth_quote: Decimal | None = None
        ask_depth_quote: Decimal | None = None
        funding_rate: float | None = None
        open_interest_usd: Decimal | None = None

        try:
            order_book = client.get_order_book(benchmark_inst_id, depth=self.settings.factor_market_state_order_book_levels)
            spread_bps = order_book.spread_bps
            bid_depth_quote = order_book.bid_depth_quote
            ask_depth_quote = order_book.ask_depth_quote
            now = order_book.ts
        except Exception as exc:
            notes.append(f"order_book_unavailable={exc}")
            self.logger.warning("Market state order book fetch failed for %s: %s", benchmark_inst_id, exc)

        try:
            funding = client.get_funding_rate(benchmark_swap_inst_id)
            funding_rate = float(funding.funding_rate)
            now = max(now, funding.ts)
        except Exception as exc:
            notes.append(f"funding_unavailable={exc}")
            self.logger.warning("Market state funding fetch failed for %s: %s", benchmark_swap_inst_id, exc)

        try:
            open_interest = client.get_open_interest(benchmark_swap_inst_id, inst_type="SWAP")
            open_interest_usd = open_interest.open_interest_usd
            now = max(now, open_interest.ts)
        except Exception as exc:
            notes.append(f"open_interest_unavailable={exc}")
            self.logger.warning("Market state open interest fetch failed for %s: %s", benchmark_swap_inst_id, exc)

        return PublicMarketStateSnapshot(
            benchmark_inst_id=benchmark_inst_id,
            benchmark_swap_inst_id=benchmark_swap_inst_id,
            spread_bps=spread_bps,
            bid_depth_quote=bid_depth_quote,
            ask_depth_quote=ask_depth_quote,
            funding_rate=funding_rate,
            open_interest_usd=open_interest_usd,
            ts=now,
            notes=tuple(notes),
        )

    def snapshot(
        self,
        market_data: dict[str, list[Candle]],
        benchmark_data: list[Candle],
        *,
        public_state: PublicMarketStateSnapshot | None = None,
    ) -> MarketStateSnapshot:
        benchmark = self._confirmed(benchmark_data)
        benchmark_swap_inst_id = (
            public_state.benchmark_swap_inst_id if public_state is not None else self._benchmark_swap_inst_id()
        )
        if len(benchmark) < self._required_benchmark_history():
            return MarketStateSnapshot(
                benchmark_inst_id=self.settings.factor_benchmark_inst_id,
                benchmark_swap_inst_id=benchmark_swap_inst_id,
                ts=public_state.ts if public_state is not None else datetime.now(UTC),
                breadth=0.0,
                benchmark_momentum=0.0,
                benchmark_slow_gap=0.0,
                benchmark_volatility=0.0,
                momentum_dispersion=0.0,
                median_volume_ratio=0.0,
                spread_bps=public_state.spread_bps if public_state is not None else None,
                bid_depth_quote=public_state.bid_depth_quote if public_state is not None else None,
                ask_depth_quote=public_state.ask_depth_quote if public_state is not None else None,
                funding_rate=public_state.funding_rate if public_state is not None else None,
                open_interest_usd=public_state.open_interest_usd if public_state is not None else None,
                risk_score=0.0,
                exposure_multiplier=float(self.settings.factor_market_state_min_exposure),
                entries_allowed=False,
                reduce_only=True,
                reason="insufficient benchmark history",
                notes=public_state.notes if public_state is not None else (),
            )

        benchmark_closes = self._close_series(benchmark)
        benchmark_slow_ma = self._sma(benchmark_closes, self.settings.factor_regime_slow_ma)
        benchmark_momentum = self._return(benchmark_closes, self.settings.factor_regime_momentum_lookback)
        benchmark_slow_gap = benchmark_closes[-1] / benchmark_slow_ma - 1.0
        benchmark_volatility = self._volatility(benchmark_closes, self.settings.factor_volatility_lookback)

        breadth_pass = 0
        breadth_total = 0
        medium_momentums: list[float] = []
        volume_ratios: list[float] = []
        for inst_id, candles in market_data.items():
            confirmed = self._confirmed(candles)
            if len(confirmed) < self.settings.factor_min_history:
                continue
            closes = self._close_series(confirmed)
            volumes = self._volume_series(confirmed)
            medium_momentum = self._return(closes, self.settings.factor_medium_lookback)
            slow_ma = self._sma(closes, self.settings.factor_slow_ma)
            slow_gap = closes[-1] / slow_ma - 1.0
            trailing_volume = self._sma(volumes[:-1], self.settings.factor_volume_lookback)
            volume_ratio = volumes[-1] / max(trailing_volume, 1e-6)
            if inst_id != self.settings.factor_benchmark_inst_id:
                breadth_total += 1
                if medium_momentum > 0 and slow_gap > 0:
                    breadth_pass += 1
            medium_momentums.append(medium_momentum)
            volume_ratios.append(volume_ratio)

        breadth = breadth_pass / breadth_total if breadth_total else 0.0
        momentum_dispersion = pstdev(medium_momentums) if len(medium_momentums) > 1 else 0.0
        median_volume_ratio = float(median(volume_ratios)) if volume_ratios else 0.0

        score = 1.0
        reasons: list[str] = []
        if benchmark_slow_gap < 0:
            score *= 0.75
            reasons.append(f"benchmark_below_slow_ma={benchmark_slow_gap:.2%}")
        if benchmark_momentum < float(self.settings.factor_market_state_min_benchmark_momentum):
            score *= 0.85
            reasons.append(f"benchmark_momentum={benchmark_momentum:.2%}")
        if breadth < float(self.settings.factor_market_state_min_breadth):
            score *= 0.80
            reasons.append(f"breadth={breadth:.2%}")
        if momentum_dispersion > float(self.settings.factor_market_state_max_momentum_dispersion):
            score *= 0.90
            reasons.append(f"dispersion={momentum_dispersion:.2f}")
        if median_volume_ratio < float(self.settings.factor_market_state_min_volume_ratio):
            score *= 0.90
            reasons.append(f"volume_ratio={median_volume_ratio:.2f}")

        notes = list(public_state.notes) if public_state is not None else []
        spread_bps = public_state.spread_bps if public_state is not None else None
        bid_depth_quote = public_state.bid_depth_quote if public_state is not None else None
        ask_depth_quote = public_state.ask_depth_quote if public_state is not None else None
        funding_rate = public_state.funding_rate if public_state is not None else None
        open_interest_usd = public_state.open_interest_usd if public_state is not None else None

        if spread_bps is not None and spread_bps > float(self.settings.factor_market_state_max_spread_bps):
            score *= 0.70
            reasons.append(f"spread_bps={spread_bps:.2f}")
        if bid_depth_quote is not None and ask_depth_quote is not None:
            min_depth = min(bid_depth_quote, ask_depth_quote)
            if min_depth < self.settings.factor_market_state_min_depth_quote:
                score *= 0.70
                reasons.append(f"depth_quote={min_depth}")
        if funding_rate is not None and abs(funding_rate) > float(self.settings.factor_market_state_max_abs_funding_rate):
            score *= 0.80
            reasons.append(f"funding_rate={funding_rate:.4%}")
        if (
            open_interest_usd is not None
            and self.settings.factor_market_state_min_open_interest_usd > 0
            and open_interest_usd < self.settings.factor_market_state_min_open_interest_usd
        ):
            score *= 0.85
            reasons.append(f"open_interest_usd={open_interest_usd}")

        score = max(0.0, min(score, 1.0))
        exposure_multiplier = 1.0 if score >= 0.999 else max(float(self.settings.factor_market_state_min_exposure), score)
        entries_allowed = (
            score >= float(self.settings.factor_market_state_entry_gate)
            and benchmark_slow_gap >= 0
            and benchmark_momentum >= float(self.settings.factor_market_state_min_benchmark_momentum)
        )
        reason = "healthy" if not reasons else "; ".join(reasons)
        ts = public_state.ts if public_state is not None else benchmark[-1].ts
        return MarketStateSnapshot(
            benchmark_inst_id=self.settings.factor_benchmark_inst_id,
            benchmark_swap_inst_id=benchmark_swap_inst_id,
            ts=ts,
            breadth=breadth,
            benchmark_momentum=benchmark_momentum,
            benchmark_slow_gap=benchmark_slow_gap,
            benchmark_volatility=benchmark_volatility,
            momentum_dispersion=momentum_dispersion,
            median_volume_ratio=median_volume_ratio,
            spread_bps=spread_bps,
            bid_depth_quote=bid_depth_quote,
            ask_depth_quote=ask_depth_quote,
            funding_rate=funding_rate,
            open_interest_usd=open_interest_usd,
            risk_score=score,
            exposure_multiplier=exposure_multiplier,
            entries_allowed=entries_allowed,
            reduce_only=not entries_allowed,
            reason=reason,
            notes=tuple(notes),
        )
