from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from okx_quant.config import Settings
from okx_quant.models import Candle, SpotTicker


def _median_decimal(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal("0")
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal("2")


def _quote_volume(candle: Candle) -> Decimal:
    return candle.quote_volume or candle.volume * candle.close


def discover_factor_universe(
    settings: Settings,
    ticker_map: dict[str, SpotTicker],
    allowed_inst_ids: set[str],
    fetch_recent_candles: Callable[[str, int], list[Candle]],
) -> list[str]:
    if settings.factor_universe:
        return list(settings.factor_universe)

    suffix = f"-{settings.factor_quote_currency}"
    candidates = [
        ticker
        for ticker in ticker_map.values()
        if ticker.inst_id in allowed_inst_ids
        and ticker.inst_id.endswith(suffix)
        and ticker.last >= settings.factor_min_last_price
        and ticker.inst_id.split("-", 1)[0] not in settings.factor_excluded_bases
    ]
    candidates.sort(key=lambda item: item.quote_volume_24h, reverse=True)

    lookback = min(settings.factor_liquidity_lookback, 300)
    history_limit = min(max(settings.factor_min_history + 5, lookback + 5), 300)
    scored: list[tuple[Decimal, Decimal, Decimal, str]] = []
    for ticker in candidates[: settings.factor_universe_candidates]:
        candles = fetch_recent_candles(ticker.inst_id, history_limit)
        confirmed = [candle for candle in candles if candle.confirmed]
        if len(confirmed) < max(settings.factor_min_history, lookback):
            continue
        volumes = [_quote_volume(candle) for candle in confirmed[-lookback:]]
        median_volume = _median_decimal(volumes)
        if median_volume < settings.factor_min_24h_quote_volume:
            continue
        average_volume = sum(volumes) / Decimal(len(volumes))
        scored.append((median_volume, average_volume, ticker.quote_volume_24h, ticker.inst_id))

    scored.sort(reverse=True)
    return [inst_id for _, _, _, inst_id in scored[: settings.factor_max_universe_size]]
