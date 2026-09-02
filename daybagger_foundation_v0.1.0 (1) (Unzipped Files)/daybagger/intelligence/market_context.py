from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from daybagger.data.upstox import IntradayCandle, UpstoxMarketData


class MarketContextError(RuntimeError):
    """Market context could not be computed from genuine observations."""


@dataclass(frozen=True, slots=True)
class ContextFeatures:
    instrument_key: str
    bars: int
    session_return_bps: float
    return_5m_bps: float | None
    return_15m_bps: float | None
    return_30m_bps: float | None
    session_range_bps: float
    trend_efficiency: float
    close_location: float

    def as_dict(self) -> dict[str, float | int | None | str]:
        return {
            "instrument_key": self.instrument_key,
            "bars": self.bars,
            "session_return_bps": self.session_return_bps,
            "return_5m_bps": self.return_5m_bps,
            "return_15m_bps": self.return_15m_bps,
            "return_30m_bps": self.return_30m_bps,
            "session_range_bps": self.session_range_bps,
            "trend_efficiency": self.trend_efficiency,
            "close_location": self.close_location,
        }


class MarketContextEngine:
    """
    Threshold-free market context.

    It does NOT decide bullish/bearish, trade/no-trade, or model weights.
    It only converts genuine candles into reusable numerical evidence.
    """

    def __init__(self, market_data: UpstoxMarketData):
        self.market_data = market_data

    def build(
        self,
        instrument_keys: Sequence[str],
        *,
        interval_minutes: int = 1,
    ) -> dict[str, ContextFeatures]:
        keys = [str(key).strip() for key in instrument_keys if str(key).strip()]
        if not keys:
            raise MarketContextError("at least one context instrument is required")

        result: dict[str, ContextFeatures] = {}
        for key in dict.fromkeys(keys):
            candles = self.market_data.intraday_candles(
                key,
                interval_minutes=interval_minutes,
            )
            result[key] = compute_context_features(candles)
        return result


def compute_context_features(candles: Sequence[IntradayCandle]) -> ContextFeatures:
    if not candles:
        raise MarketContextError("cannot compute context without candles")

    bars = sorted(candles, key=lambda item: item.timestamp)
    key = bars[0].instrument_key
    if any(item.instrument_key != key for item in bars):
        raise MarketContextError("mixed instrument keys in one context series")

    session_open = bars[0].open
    last_close = bars[-1].close
    session_high = max(item.high for item in bars)
    session_low = min(item.low for item in bars)

    if session_open <= 0:
        raise MarketContextError("session open must be positive")

    session_return = _bps(last_close, session_open)
    session_range = float(
        (session_high - session_low) / session_open * Decimal("10000")
    )

    path = [bars[0].open] + [item.close for item in bars]
    travelled = sum(abs(path[i] - path[i - 1]) for i in range(1, len(path)))
    net_move = abs(last_close - session_open)
    efficiency = float(net_move / travelled) if travelled > 0 else 0.0

    full_range = session_high - session_low
    close_location = (
        float((last_close - session_low) / full_range)
        if full_range > 0
        else 0.5
    )

    return ContextFeatures(
        instrument_key=key,
        bars=len(bars),
        session_return_bps=session_return,
        return_5m_bps=_window_return_bps(bars, 5),
        return_15m_bps=_window_return_bps(bars, 15),
        return_30m_bps=_window_return_bps(bars, 30),
        session_range_bps=session_range,
        trend_efficiency=max(0.0, min(1.0, efficiency)),
        close_location=max(0.0, min(1.0, close_location)),
    )


def _window_return_bps(
    bars: Sequence[IntradayCandle],
    window_bars: int,
) -> float | None:
    if len(bars) < window_bars:
        return None
    window = bars[-window_bars:]
    return _bps(window[-1].close, window[0].open)


def _bps(end: Decimal, start: Decimal) -> float:
    if start <= 0:
        raise MarketContextError("price denominator must be positive")
    return float((end / start - Decimal("1")) * Decimal("10000"))
