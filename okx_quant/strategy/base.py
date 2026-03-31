from __future__ import annotations

from abc import ABC, abstractmethod

from okx_quant.models import Candle, Signal


class Strategy(ABC):
    @abstractmethod
    def generate(self, candles: list[Candle]) -> Signal:
        raise NotImplementedError
