from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from statistics import median, pstdev
from typing import Mapping, Sequence

from daybagger.data.upstox import IntradayCandle
from daybagger.intelligence.engine import time_normalized_volume_features
from daybagger.intelligence.market_context import compute_context_features
from daybagger.specialists.features import stock_price_features


class MetaFeatureError(RuntimeError):
    """Timestamp-aligned meta intelligence cannot be built safely."""


@dataclass(frozen=True, slots=True)
class CrossSectionState:
    session_date: date
    as_of: datetime
    returns_by_symbol: Mapping[str, float]
    sector_return_by_name: Mapping[str, float]
    sector_percentile_by_name: Mapping[str, float]
    advance_ratio: float
    median_return_bps: float
    dispersion_bps: float


def build_cross_section_state(
    *,
    session_date: date,
    as_of: datetime,
    prefixes_by_symbol: Mapping[str, Sequence[IntradayCandle]],
    sector_by_symbol: Mapping[str, str],
) -> CrossSectionState:
    """
    Build cross-sectional evidence from the SAME timestamp-aligned deep universe.

    No live-only breadth is injected here, so historical/replay/live can call the
    same logic. Symbols without a real aligned prefix are omitted rather than
    replaced by fabricated values.
    """
    if as_of.tzinfo is None:
        raise MetaFeatureError("as_of must be timezone-aware")

    returns: dict[str, float] = {}
    by_sector: dict[str, list[float]] = {}
    for symbol, candles in prefixes_by_symbol.items():
        if not candles:
            continue
        ordered = sorted(candles, key=lambda c: c.timestamp)
        if ordered[-1].timestamp != as_of:
            continue
        try:
            value = float(stock_price_features(ordered)["stock_session_return_bps"])
        except Exception:
            continue
        returns[symbol] = value
        sector = str(sector_by_symbol.get(symbol) or "").strip()
        if sector:
            by_sector.setdefault(sector, []).append(value)

    if len(returns) < 3:
        raise MetaFeatureError("cross-section requires at least three aligned equities")

    sector_returns = {
        sector: float(median(values))
        for sector, values in by_sector.items()
        if values
    }
    sector_values = list(sector_returns.values())
    sector_percentiles = {
        sector: _percentile(value, sector_values)
        for sector, value in sector_returns.items()
    }

    population = list(returns.values())
    advances = sum(1 for value in population if value > 0)
    return CrossSectionState(
        session_date=session_date,
        as_of=as_of,
        returns_by_symbol=returns,
        sector_return_by_name=sector_returns,
        sector_percentile_by_name=sector_percentiles,
        advance_ratio=advances / len(population),
        median_return_bps=float(median(population)),
        dispersion_bps=float(pstdev(population)) if len(population) > 1 else 0.0,
    )


def build_meta_raw_features(
    *,
    symbol: str,
    stock_prefix: Sequence[IntradayCandle],
    market_prefix: Sequence[IntradayCandle],
    bank_nifty_prefix: Sequence[IntradayCandle],
    india_vix_prefix: Sequence[IntradayCandle],
    cross_section: CrossSectionState,
    sector: str,
    prior_stock_sessions: Sequence[Sequence[IntradayCandle]],
    external_numeric: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """
    Canonical raw feature builder for meta research and live paper inference.

    Every feature is computed from observations available at ``stock_prefix[-1]``.
    Historical volume baselines must contain sessions STRICTLY before the current
    session; this is checked by the caller and rechecked here where possible.
    """
    if not stock_prefix or not market_prefix or not bank_nifty_prefix or not india_vix_prefix:
        raise MetaFeatureError("stock, NIFTY, Bank NIFTY and India VIX prefixes are required")

    as_of = sorted(stock_prefix, key=lambda c: c.timestamp)[-1].timestamp
    if as_of.tzinfo is None:
        raise MetaFeatureError("feature timestamp must be timezone-aware")
    for name, series in (
        ("market", market_prefix),
        ("bank_nifty", bank_nifty_prefix),
        ("india_vix", india_vix_prefix),
    ):
        ordered = sorted(series, key=lambda c: c.timestamp)
        if ordered[-1].timestamp != as_of:
            raise MetaFeatureError(f"{name} is not timestamp-aligned with stock")

    stock = stock_price_features(stock_prefix)
    market = compute_context_features(market_prefix)
    bank = compute_context_features(bank_nifty_prefix)
    vix = compute_context_features(india_vix_prefix)

    if market.return_15m_bps is None or bank.return_15m_bps is None or vix.return_15m_bps is None:
        raise MetaFeatureError("at least 15 aligned bars are required for context")

    if symbol not in cross_section.returns_by_symbol:
        raise MetaFeatureError(f"{symbol}: missing from aligned cross-section")
    sector_name = sector.strip()
    if not sector_name or sector_name not in cross_section.sector_return_by_name:
        raise MetaFeatureError(f"{symbol}: sector evidence unavailable")

    current_date = as_of.date()
    for hist in prior_stock_sessions:
        if hist:
            hist_date = sorted(hist, key=lambda c: c.timestamp)[0].timestamp.date()
            if hist_date >= current_date:
                raise MetaFeatureError("relative-volume baseline contains current/future session")
    volume = time_normalized_volume_features(
        symbol=symbol,
        current_session=stock_prefix,
        historical_sessions=prior_stock_sessions,
    )

    stock_ret = float(stock["stock_session_return_bps"])
    population = list(cross_section.returns_by_symbol.values())
    sector_ret = float(cross_section.sector_return_by_name[sector_name])

    session_open = sorted(stock_prefix, key=lambda c: c.timestamp)[0].open
    session_high = max(c.high for c in stock_prefix)
    session_low = min(c.low for c in stock_prefix)
    stock_range_bps = float((session_high - session_low) / session_open * 10000)

    result = {
        **{key: float(value) for key, value in stock.items()},
        "stock_session_range_bps": stock_range_bps,
        "relative_volume": float(volume.relative_volume),
        "rs_vs_benchmark_bps": stock_ret - float(market.session_return_bps),
        "rs_vs_sector_bps": stock_ret - sector_ret,
        "sector_session_return_percentile": float(
            cross_section.sector_percentile_by_name[sector_name]
        ),
        "cross_section_return_percentile": _percentile(stock_ret, population),
        "cross_section_dispersion_bps": float(cross_section.dispersion_bps),
        "breadth_advance_ratio": float(cross_section.advance_ratio),
        "breadth_median_return_bps": float(cross_section.median_return_bps),
        "market_session_return_bps": float(market.session_return_bps),
        "market_return_15m_bps": float(market.return_15m_bps),
        "market_trend_efficiency": float(market.trend_efficiency),
        "market_session_range_bps": float(market.session_range_bps),
        "bank_nifty_session_return_bps": float(bank.session_return_bps),
        "bank_nifty_return_15m_bps": float(bank.return_15m_bps),
        "bank_nifty_trend_efficiency": float(bank.trend_efficiency),
        "bank_nifty_session_range_bps": float(bank.session_range_bps),
        "india_vix_session_return_bps": float(vix.session_return_bps),
        "india_vix_return_15m_bps": float(vix.return_15m_bps),
        "india_vix_session_range_bps": float(vix.session_range_bps),
    }
    if external_numeric:
        for key, value in external_numeric.items():
            if value is None:
                continue
            numeric = float(value)
            if numeric != numeric or numeric in (float("inf"), float("-inf")):
                raise MetaFeatureError(f"invalid external feature: {key}")
            result[str(key)] = numeric
    return result


def _percentile(value: float, population: Sequence[float]) -> float:
    if not population:
        raise MetaFeatureError("cannot rank against empty population")
    below = sum(1 for item in population if item < value)
    equal = sum(1 for item in population if item == value)
    return (below + 0.5 * equal) / len(population)
