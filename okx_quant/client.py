from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests
from requests import RequestException

from okx_quant.config import Settings
from okx_quant.models import (
    Candle,
    FundingRateSnapshot,
    InstrumentRules,
    OpenInterestSnapshot,
    OrderBookLevel,
    OrderBookSnapshot,
    SpotTicker,
)


class OkxApiError(RuntimeError):
    pass


class OkxRestClient:
    def __init__(self, settings: Settings, timeout: int = 15) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = timeout

    def _now(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}".encode()
        digest = hmac.new(self.settings.api_secret.encode(), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, timestamp: str, method: str, request_path: str, body: str, auth: bool) -> dict[str, str]:
        headers = {}
        if self.settings.simulated:
            headers["x-simulated-trading"] = "1"
        if auth:
            self.settings.require_private_api()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.settings.api_key,
                    "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self.settings.api_passphrase,
                }
            )
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        request_path = f"{path}{query}"
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True) if payload else ""
        retryable = method.upper() == "GET"
        for attempt in range(self.settings.http_max_retries):
            try:
                timestamp = self._now()
                response = self.session.request(
                    method=method.upper(),
                    url=f"{self.settings.base_url}{path}",
                    params=params,
                    data=body or None,
                    headers=self._headers(timestamp, method, request_path, body, auth),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code") != "0":
                    raise OkxApiError(f"OKX API error {data.get('code')}: {data.get('msg')}")
                return data.get("data", [])
            except (RequestException, OkxApiError):
                if not retryable or attempt == self.settings.http_max_retries - 1:
                    raise
                time.sleep(self.settings.http_retry_backoff_sec * (2**attempt))
        raise RuntimeError("Unreachable retry loop")

    def _parse_candle_row(self, row: list[str]) -> Candle:
        quote_volume = row[6] if len(row) > 6 else "0"
        confirmed = row[8] == "1" if len(row) > 8 else row[-1] == "1"
        return Candle(
            ts=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
            open=Decimal(row[1]),
            high=Decimal(row[2]),
            low=Decimal(row[3]),
            close=Decimal(row[4]),
            volume=Decimal(row[5]),
            quote_volume=Decimal(quote_volume or "0"),
            confirmed=confirmed,
        )

    def get_candles(self, inst_id: str, bar: str, limit: int) -> list[Candle]:
        rows = self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": limit},
        )
        candles = [self._parse_candle_row(row) for row in rows]
        candles.sort(key=lambda candle: candle.ts)
        return candles

    def get_history_candles(self, inst_id: str, bar: str, limit: int, after: str | None = None) -> list[Candle]:
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": limit}
        if after is not None:
            params["after"] = after
        rows = self._request("GET", "/api/v5/market/history-candles", params=params)
        candles = [self._parse_candle_row(row) for row in rows]
        candles.sort(key=lambda candle: candle.ts)
        return candles

    def get_history_candles_paginated(self, inst_id: str, bar: str, limit: int) -> list[Candle]:
        all_candles: list[Candle] = []
        seen: set[datetime] = set()
        after: str | None = None
        while len(all_candles) < limit:
            batch_size = min(300, limit - len(all_candles))
            batch = self.get_history_candles(inst_id, bar, batch_size, after=after)
            if not batch:
                break
            for candle in batch:
                if candle.ts in seen:
                    continue
                seen.add(candle.ts)
                all_candles.append(candle)
            oldest_ts = min(candle.ts for candle in batch)
            after = str(int(oldest_ts.timestamp() * 1000))
            if len(batch) < batch_size:
                break
            time.sleep(0.12)
        all_candles.sort(key=lambda candle: candle.ts)
        return all_candles[-limit:]

    def get_instrument_rules(self, inst_id: str) -> InstrumentRules:
        rows = self._request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": inst_id},
        )
        if not rows:
            raise OkxApiError(f"No instrument metadata returned for {inst_id}")
        item = rows[0]
        return InstrumentRules(
            min_size=Decimal(item["minSz"]),
            lot_size=Decimal(item["lotSz"]),
            tick_size=Decimal(item["tickSz"]),
        )

    def list_spot_instruments(self, quote_ccy: str | None = None) -> list[dict[str, Any]]:
        rows = self._request("GET", "/api/v5/public/instruments", params={"instType": "SPOT"})
        if quote_ccy is None:
            return rows
        return [row for row in rows if row.get("quoteCcy") == quote_ccy]

    def get_spot_tickers(self) -> list[SpotTicker]:
        rows = self._request("GET", "/api/v5/market/tickers", params={"instType": "SPOT"})
        tickers: list[SpotTicker] = []
        for row in rows:
            last = row.get("last") or "0"
            try:
                last_price = Decimal(last)
            except Exception:
                continue
            if last_price <= 0:
                continue
            tickers.append(
                SpotTicker(
                    inst_id=row["instId"],
                    last=last_price,
                    quote_volume_24h=Decimal(row.get("volCcy24h") or "0"),
                    base_volume_24h=Decimal(row.get("vol24h") or "0"),
                    bid=Decimal(row.get("bidPx") or "0"),
                    ask=Decimal(row.get("askPx") or "0"),
                )
            )
        return tickers

    def get_order_book(self, inst_id: str, depth: int = 5) -> OrderBookSnapshot:
        rows = self._request("GET", "/api/v5/market/books", params={"instId": inst_id, "sz": depth})
        if not rows:
            raise OkxApiError(f"No order book returned for {inst_id}")
        row = rows[0]
        bids = tuple(OrderBookLevel(price=Decimal(level[0]), size=Decimal(level[1])) for level in row.get("bids", []))
        asks = tuple(OrderBookLevel(price=Decimal(level[0]), size=Decimal(level[1])) for level in row.get("asks", []))
        best_bid = bids[0].price if bids else Decimal("0")
        best_ask = asks[0].price if asks else Decimal("0")
        mid = (best_bid + best_ask) / Decimal("2") if best_bid > 0 and best_ask > 0 else Decimal("0")
        spread_bps = float((best_ask - best_bid) / mid * Decimal("10000")) if mid > 0 else 0.0
        bid_depth_quote = sum(level.price * level.size for level in bids)
        ask_depth_quote = sum(level.price * level.size for level in asks)
        return OrderBookSnapshot(
            inst_id=inst_id,
            ts=datetime.fromtimestamp(int(row["ts"]) / 1000, tz=UTC),
            bids=bids,
            asks=asks,
            spread_bps=spread_bps,
            bid_depth_quote=bid_depth_quote,
            ask_depth_quote=ask_depth_quote,
        )

    def get_funding_rate(self, inst_id: str) -> FundingRateSnapshot:
        rows = self._request("GET", "/api/v5/public/funding-rate", params={"instId": inst_id})
        if not rows:
            raise OkxApiError(f"No funding rate returned for {inst_id}")
        row = rows[0]
        next_funding_time_raw = row.get("nextFundingTime") or ""
        return FundingRateSnapshot(
            inst_id=row.get("instId", inst_id),
            funding_rate=Decimal(row.get("fundingRate") or "0"),
            ts=datetime.fromtimestamp(int(row["ts"]) / 1000, tz=UTC),
            next_funding_time=(
                datetime.fromtimestamp(int(next_funding_time_raw) / 1000, tz=UTC)
                if next_funding_time_raw
                else None
            ),
        )

    def get_open_interest(self, inst_id: str, inst_type: str = "SWAP") -> OpenInterestSnapshot:
        rows = self._request("GET", "/api/v5/public/open-interest", params={"instType": inst_type, "instId": inst_id})
        if not rows:
            raise OkxApiError(f"No open interest returned for {inst_id}")
        row = rows[0]
        return OpenInterestSnapshot(
            inst_id=row.get("instId", inst_id),
            open_interest=Decimal(row.get("oi") or "0"),
            open_interest_ccy=Decimal(row.get("oiCcy") or "0"),
            open_interest_usd=Decimal(row.get("oiUsd") or "0"),
            ts=datetime.fromtimestamp(int(row["ts"]) / 1000, tz=UTC),
        )

    def get_all_balances(self) -> dict[str, Decimal]:
        rows = self._request("GET", "/api/v5/account/balance", auth=True)
        if not rows:
            return {}
        result: dict[str, Decimal] = {}
        for item in rows[0].get("details", []):
            ccy = item.get("ccy")
            if not ccy:
                continue
            result[ccy] = Decimal(item.get("availBal") or item.get("cashBal") or "0")
        return result

    def get_balances(self, currencies: list[str]) -> dict[str, Decimal]:
        all_balances = self.get_all_balances()
        return {ccy: all_balances.get(ccy, Decimal("0")) for ccy in currencies}

    def place_market_order(self, inst_id: str, side: str, size: Decimal, target_currency: str) -> dict[str, Any]:
        payload = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "market",
            "sz": format(size, "f"),
            "tgtCcy": target_currency,
            "clOrdId": f"bot-{int(datetime.now(UTC).timestamp())}",
        }
        rows = self._request("POST", "/api/v5/trade/order", payload=payload, auth=True)
        if not rows:
            raise OkxApiError("OKX returned no order payload")
        return rows[0]
