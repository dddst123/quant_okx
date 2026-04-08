from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class SignalAction(StrEnum):
    HOLD = "hold"
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    confirmed: bool


@dataclass(frozen=True)
class Signal:
    action: SignalAction
    price: Decimal
    reason: str


@dataclass(frozen=True)
class InstrumentRules:
    min_size: Decimal
    lot_size: Decimal
    tick_size: Decimal


@dataclass(frozen=True)
class SpotTicker:
    inst_id: str
    last: Decimal
    quote_volume_24h: Decimal
    base_volume_24h: Decimal
    bid: Decimal = Decimal("0")
    ask: Decimal = Decimal("0")


@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBookSnapshot:
    inst_id: str
    ts: datetime
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    spread_bps: float
    bid_depth_quote: Decimal
    ask_depth_quote: Decimal


@dataclass(frozen=True)
class FundingRateSnapshot:
    inst_id: str
    funding_rate: Decimal
    ts: datetime
    next_funding_time: datetime | None


@dataclass(frozen=True)
class OpenInterestSnapshot:
    inst_id: str
    open_interest: Decimal
    open_interest_ccy: Decimal
    open_interest_usd: Decimal
    ts: datetime


@dataclass(frozen=True)
class AccountSnapshot:
    base_balance: Decimal
    quote_balance: Decimal
    base_market_value: Decimal


@dataclass(frozen=True)
class OrderIntent:
    side: str
    size: Decimal
    target_currency: str
    reason: str
