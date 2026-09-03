from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from statistics import median
from typing import Mapping, Sequence

from daybagger.data.universe import ObservableEquity
from daybagger.data.upstox import IntradayCandle, UpstoxQuoteSnapshot
from daybagger.intelligence.market_context import ContextFeatures


class IntelligenceError(RuntimeError):
    """Observed intelligence inputs are missing or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class BreadthFeatures:
    observed_stocks: int
    advances: int
    declines: int
    unchanged: int
    advance_ratio: float
    median_session_return_bps: float
    pct_above_session_open: float
    pct_with_two_sided_quote: float
    median_spread_bps: float | None
    total_session_turnover_inr: Decimal


@dataclass(frozen=True, slots=True)
class MicrostructureFeatures:
    symbol: str
    spread_bps: float | None
    buy_sell_quantity_imbalance: float | None
    best_bid: Decimal | None
    best_ask: Decimal | None


@dataclass(frozen=True, slots=True)
class RelativeStrengthFeatures:
    symbol: str
    stock_session_return_bps: float
    versus_benchmark_bps: float
    versus_sector_bps: float | None


@dataclass(frozen=True, slots=True)
class TimeNormalizedVolumeFeatures:
    symbol: str
    current_cumulative_volume: int
    comparable_sessions: int
    historical_median_cumulative_volume: float
    relative_volume: float


@dataclass(frozen=True, slots=True)
class SectorStrengthFeatures:
    sector_key: str
    session_return_bps: float
    return_15m_bps: float | None
    trend_efficiency: float
    session_return_percentile: float
    return_15m_percentile: float | None
    trend_efficiency_percentile: float


def breadth_features(observed: Sequence[ObservableEquity]) -> BreadthFeatures:
    if not observed:
        raise IntelligenceError("breadth requires at least one observed equity")

    returns: list[float] = []
    spreads: list[float] = []
    advances = declines = unchanged = 0
    above_open = 0
    two_sided = 0
    total_turnover = Decimal("0")

    for item in observed:
        snap = item.quote
        if snap.session_open <= 0:
            raise IntelligenceError(f"{snap.symbol}: invalid session open")

        ret = _bps(snap.last_price, snap.session_open)
        returns.append(ret)

        if ret > 0:
            advances += 1
            above_open += 1
        elif ret < 0:
            declines += 1
        else:
            unchanged += 1

        if snap.best_bid is not None and snap.best_ask is not None:
            two_sided += 1
            if item.spread_bps is not None and isfinite(item.spread_bps):
                spreads.append(item.spread_bps)

        total_turnover += item.session_turnover_inr

    n = len(observed)
    return BreadthFeatures(
        observed_stocks=n,
        advances=advances,
        declines=declines,
        unchanged=unchanged,
        advance_ratio=advances / n,
        median_session_return_bps=float(median(returns)),
        pct_above_session_open=above_open / n,
        pct_with_two_sided_quote=two_sided / n,
        median_spread_bps=float(median(spreads)) if spreads else None,
        total_session_turnover_inr=total_turnover,
    )


def microstructure_features(snapshot: UpstoxQuoteSnapshot) -> MicrostructureFeatures:
    spread = None
    if snapshot.best_bid is not None and snapshot.best_ask is not None:
        mid = (snapshot.best_bid + snapshot.best_ask) / Decimal("2")
        if mid > 0:
            spread = float(
                (snapshot.best_ask - snapshot.best_bid)
                / mid
                * Decimal("10000")
            )

    imbalance = None
    buy = snapshot.total_buy_quantity
    sell = snapshot.total_sell_quantity
    if buy is not None and sell is not None and (buy + sell) > 0:
        imbalance = (buy - sell) / (buy + sell)

    return MicrostructureFeatures(
        symbol=snapshot.symbol,
        spread_bps=spread,
        buy_sell_quantity_imbalance=imbalance,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
    )


def relative_strength_features(
    *,
    snapshot: UpstoxQuoteSnapshot,
    benchmark: ContextFeatures,
    sector: ContextFeatures | None = None,
) -> RelativeStrengthFeatures:
    stock_ret = _bps(snapshot.last_price, snapshot.session_open)
    return RelativeStrengthFeatures(
        symbol=snapshot.symbol,
        stock_session_return_bps=stock_ret,
        versus_benchmark_bps=stock_ret - benchmark.session_return_bps,
        versus_sector_bps=(
            stock_ret - sector.session_return_bps if sector is not None else None
        ),
    )


def time_normalized_volume_features(
    *,
    symbol: str,
    current_session: Sequence[IntradayCandle],
    historical_sessions: Sequence[Sequence[IntradayCandle]],
) -> TimeNormalizedVolumeFeatures:
    """
    Compare today's cumulative volume only with historical sessions observed
    through the SAME bar count.

    This avoids comparing morning volume with full-day average volume.
    """
    if not current_session:
        raise IntelligenceError("current_session cannot be empty")

    current = sorted(current_session, key=lambda c: c.timestamp)
    current_count = len(current)
    current_volume = sum(int(c.volume) for c in current)

    comparable: list[int] = []
    for session in historical_sessions:
        ordered = sorted(session, key=lambda c: c.timestamp)
        if len(ordered) < current_count:
            continue
        comparable.append(sum(int(c.volume) for c in ordered[:current_count]))

    if not comparable:
        raise IntelligenceError(
            f"{symbol}: no historical sessions are long enough for time-normalized volume"
        )

    baseline = float(median(comparable))
    if baseline <= 0:
        raise IntelligenceError(f"{symbol}: historical volume baseline is not positive")

    return TimeNormalizedVolumeFeatures(
        symbol=symbol,
        current_cumulative_volume=current_volume,
        comparable_sessions=len(comparable),
        historical_median_cumulative_volume=baseline,
        relative_volume=current_volume / baseline,
    )


def sector_strength_features(
    sector_contexts: Mapping[str, ContextFeatures],
) -> list[SectorStrengthFeatures]:
    """
    Cross-sectional sector ranking with NO composite score.

    Each raw dimension gets its own percentile so future models can decide
    which dimensions matter under which regime.
    """
    if not sector_contexts:
        raise IntelligenceError("sector_contexts cannot be empty")

    items = list(sector_contexts.items())
    session_values = [ctx.session_return_bps for _, ctx in items]
    efficiency_values = [ctx.trend_efficiency for _, ctx in items]

    result: list[SectorStrengthFeatures] = []
    for key, ctx in items:
        result.append(
            SectorStrengthFeatures(
                sector_key=key,
                session_return_bps=ctx.session_return_bps,
                return_15m_bps=ctx.return_15m_bps,
                trend_efficiency=ctx.trend_efficiency,
                session_return_percentile=_percentile_rank(
                    ctx.session_return_bps, session_values
                ),
                return_15m_percentile=(
                    _percentile_rank(
                        ctx.return_15m_bps,
                        [v.return_15m_bps for _, v in items if v.return_15m_bps is not None],
                    )
                    if ctx.return_15m_bps is not None
                    else None
                ),
                trend_efficiency_percentile=_percentile_rank(
                    ctx.trend_efficiency, efficiency_values
                ),
            )
        )

    return sorted(
        result,
        key=lambda x: (
            x.session_return_percentile,
            x.trend_efficiency_percentile,
        ),
        reverse=True,
    )


def _bps(end: Decimal, start: Decimal) -> float:
    if start <= 0:
        raise IntelligenceError("return denominator must be positive")
    return float((end / start - Decimal("1")) * Decimal("10000"))


def _percentile_rank(value: float, population: Sequence[float]) -> float:
    if not population:
        raise IntelligenceError("cannot rank against empty population")
    below = sum(1 for item in population if item < value)
    equal = sum(1 for item in population if item == value)
    return (below + 0.5 * equal) / len(population)
