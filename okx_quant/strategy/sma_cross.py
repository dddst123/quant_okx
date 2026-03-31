from __future__ import annotations

from decimal import Decimal

from okx_quant.models import Candle, Signal, SignalAction
from okx_quant.strategy.base import Strategy


class SmaCrossStrategy(Strategy):
    def __init__(self, fast_window: int, slow_window: int) -> None:
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        self.fast_window = fast_window
        self.slow_window = slow_window

    def _sma(self, candles: list[Candle], window: int, offset: int = 0) -> Decimal:
        subset = candles[-(window + offset) : len(candles) - offset if offset else None]
        closes = [candle.close for candle in subset]
        return sum(closes, start=Decimal("0")) / Decimal(window)

    def generate(self, candles: list[Candle]) -> Signal:
        needed = self.slow_window + 1
        if len(candles) < needed:
            raise ValueError(f"Need at least {needed} candles, got {len(candles)}")

        last_price = candles[-1].close
        fast_prev = self._sma(candles, self.fast_window, offset=1)
        slow_prev = self._sma(candles, self.slow_window, offset=1)
        fast_now = self._sma(candles, self.fast_window)
        slow_now = self._sma(candles, self.slow_window)

        if fast_prev <= slow_prev and fast_now > slow_now:
            return Signal(SignalAction.BUY, last_price, f"Golden cross {fast_now:.4f} > {slow_now:.4f}")
        if fast_prev >= slow_prev and fast_now < slow_now:
            return Signal(SignalAction.SELL, last_price, f"Death cross {fast_now:.4f} < {slow_now:.4f}")
        return Signal(SignalAction.HOLD, last_price, f"No cross {fast_now:.4f} vs {slow_now:.4f}")
