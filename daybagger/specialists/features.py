from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from daybagger.data.upstox import IntradayCandle


class SpecialistFeatureError(RuntimeError):
    """A specialist feature vector cannot be built from the supplied observations."""


def stock_price_features(
    candles: Sequence[IntradayCandle],
) -> dict[str, float]:
    """Canonical threshold-free stock features from genuine minute candles."""
    if not candles:
        raise SpecialistFeatureError("stock candles are required")
    bars = sorted(candles, key=lambda c: c.timestamp)
    key = bars[0].instrument_key
    if any(c.instrument_key != key for c in bars):
        raise SpecialistFeatureError("mixed instrument keys")

    session_open = bars[0].open
    last = bars[-1].close
    if session_open <= 0:
        raise SpecialistFeatureError("invalid session open")

    cumulative_volume = sum(c.volume for c in bars)
    if cumulative_volume <= 0:
        raise SpecialistFeatureError("positive traded volume is required for VWAP")

    numerator = sum(
        ((c.high + c.low + c.close) / Decimal("3")) * Decimal(c.volume)
        for c in bars
    )
    vwap = numerator / Decimal(cumulative_volume)

    closes = [bars[0].open] + [c.close for c in bars]
    travelled = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    trend_eff = float(abs(last - session_open) / travelled) if travelled > 0 else 0.0

    return {
        "stock_session_return_bps": _bps(last, session_open),
        "stock_return_5m_bps": _window_return(bars, 5),
        "stock_return_15m_bps": _window_return(bars, 15),
        "stock_return_30m_bps": _window_return(bars, 30),
        "stock_vwap_distance_bps": _bps(last, vwap),
        "stock_trend_efficiency": max(0.0, min(1.0, trend_eff)),
        "stock_close_location": _close_location(bars),
    }


def _window_return(bars: Sequence[IntradayCandle], n: int) -> float:
    if len(bars) < n:
        raise SpecialistFeatureError(
            f"at least {n} minute bars are required for this feature vector"
        )
    window = bars[-n:]
    return _bps(window[-1].close, window[0].open)


def _close_location(bars: Sequence[IntradayCandle]) -> float:
    high = max(c.high for c in bars)
    low = min(c.low for c in bars)
    if high == low:
        return 0.5
    return float((bars[-1].close - low) / (high - low))


def _bps(end: Decimal, start: Decimal) -> float:
    if start <= 0:
        raise SpecialistFeatureError("positive denominator required")
    return float((end / start - Decimal("1")) * Decimal("10000"))
