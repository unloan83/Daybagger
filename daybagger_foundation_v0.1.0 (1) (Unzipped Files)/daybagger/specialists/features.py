from __future__ import annotations

from decimal import Decimal
from typing import Mapping, Sequence

from daybagger.data.upstox import IntradayCandle
from daybagger.intelligence.engine import (
    BreadthFeatures,
    MicrostructureFeatures,
    RelativeStrengthFeatures,
    SectorStrengthFeatures,
    TimeNormalizedVolumeFeatures,
)
from daybagger.intelligence.market_context import ContextFeatures


class SpecialistFeatureError(RuntimeError):
    """A specialist feature vector cannot be built from the supplied observations."""


def stock_price_features(
    candles: Sequence[IntradayCandle],
) -> dict[str, float]:
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

    # Minute-bar typical-price VWAP. This is derived only from genuine minute bars.
    numerator = sum(
        ((c.high + c.low + c.close) / Decimal("3")) * Decimal(c.volume)
        for c in bars
    )
    vwap = numerator / Decimal(cumulative_volume)

    closes = [bars[0].open] + [c.close for c in bars]
    travelled = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes)))
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


def flatten_stock_features(
    *,
    stock_candles: Sequence[IntradayCandle],
    market: ContextFeatures,
    bank_nifty: ContextFeatures | None,
    india_vix: ContextFeatures | None,
    breadth: BreadthFeatures | None,
    relative_strength: RelativeStrengthFeatures,
    microstructure: MicrostructureFeatures,
    volume: TimeNormalizedVolumeFeatures | None,
    sector_strength: SectorStrengthFeatures | None,
    external_numeric: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """
    One flat, timestamp-aligned numeric feature vector.

    Missing optional intelligence stays missing. No favourable default is inserted.
    Specialist models that require absent features must fail closed.
    """
    f = stock_price_features(stock_candles)
    f.update(
        {
            "market_session_return_bps": market.session_return_bps,
            "market_return_5m_bps": _none_to_missing(market.return_5m_bps),
            "market_return_15m_bps": _none_to_missing(market.return_15m_bps),
            "market_trend_efficiency": market.trend_efficiency,
            "rs_vs_benchmark_bps": relative_strength.versus_benchmark_bps,
        }
    )

    if relative_strength.versus_sector_bps is not None:
        f["rs_vs_sector_bps"] = relative_strength.versus_sector_bps

    if bank_nifty is not None:
        f["bank_nifty_session_return_bps"] = bank_nifty.session_return_bps
        f["bank_nifty_trend_efficiency"] = bank_nifty.trend_efficiency

    if india_vix is not None:
        f["india_vix_session_return_bps"] = india_vix.session_return_bps
        f["india_vix_return_15m_bps"] = _none_to_missing(india_vix.return_15m_bps)

    if breadth is not None:
        f["breadth_advance_ratio"] = breadth.advance_ratio
        f["breadth_median_return_bps"] = breadth.median_session_return_bps
        f["breadth_two_sided_quote_ratio"] = breadth.pct_with_two_sided_quote

    if microstructure.spread_bps is not None:
        f["spread_bps"] = microstructure.spread_bps
    if microstructure.buy_sell_quantity_imbalance is not None:
        f["buy_sell_quantity_imbalance"] = microstructure.buy_sell_quantity_imbalance

    if volume is not None:
        f["relative_volume"] = volume.relative_volume

    if sector_strength is not None:
        f["sector_session_return_percentile"] = sector_strength.session_return_percentile
        f["sector_trend_efficiency_percentile"] = sector_strength.trend_efficiency_percentile
        if sector_strength.return_15m_percentile is not None:
            f["sector_return_15m_percentile"] = sector_strength.return_15m_percentile

    if external_numeric:
        for key, value in external_numeric.items():
            if value is not None:
                f[str(key)] = float(value)

    return {k: float(v) for k, v in f.items() if v is not None}


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


def _none_to_missing(value):
    return value if value is not None else None
