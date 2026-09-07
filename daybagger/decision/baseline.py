from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

from daybagger.domain import DecisionStatus, Direction, ModelOpinion, Opportunity
from daybagger.integration.costs import IndiaEquityIntradayCostModel


BASELINE_MODEL_ID = "baseline_relative_strength"
BASELINE_MODEL_VERSION = "baseline-v1"
BASELINE_VALIDATION_ID = "baseline-runtime-v1"
BASELINE_HORIZON_MINUTES = 15


@dataclass(frozen=True, slots=True)
class BaselineDecision:
    opportunity: Opportunity
    opinions: tuple[ModelOpinion, ...]
    meta_features: Mapping[str, float]
    estimated_total_cost_bps: float


def decide_baseline(
    *,
    symbol: str,
    as_of,
    raw_features: Mapping[str, float],
    statutory_cost_bps: float,
    live_spread_bps: float,
    paper_slippage_bps_per_side: float,
) -> BaselineDecision:
    """Build the deterministic, paper-only relative-strength baseline.

    The score is residual strength versus both the market and the stock's
    sector.  Direction is permitted only when the market regime, breadth,
    relative volume, spread and cost hurdle all have genuine evidence.
    """
    required = (
        "rs_vs_benchmark_bps",
        "rs_vs_sector_bps",
        "cross_section_return_percentile",
        "relative_volume",
        "market_trend_efficiency",
        "market_session_return_bps",
        "breadth_advance_ratio",
        "stock_session_range_bps",
    )
    missing = [name for name in required if name not in raw_features]
    if missing:
        return _reject(symbol, as_of, raw_features, f"MISSING_BASELINE_FEATURES:{','.join(missing)}", 0.0)

    values = {name: float(raw_features[name]) for name in required}
    if any(not isfinite(value) for value in values.values()):
        return _reject(symbol, as_of, raw_features, "NON_FINITE_BASELINE_FEATURE", 0.0)
    if statutory_cost_bps < 0 or live_spread_bps < 0 or paper_slippage_bps_per_side < 0:
        return _reject(symbol, as_of, raw_features, "INVALID_COST_EVIDENCE", 0.0)

    total_cost = float(statutory_cost_bps + live_spread_bps + 2.0 * paper_slippage_bps_per_side)
    residual_score = 0.5 * (
        values["rs_vs_benchmark_bps"] + values["rs_vs_sector_bps"]
    )
    direction = Direction.LONG if residual_score > 0 else Direction.SHORT
    percentile = values["cross_section_return_percentile"]
    breadth = values["breadth_advance_ratio"]
    market_return = values["market_session_return_bps"]

    if values["market_trend_efficiency"] < 0.10:
        return _reject(symbol, as_of, raw_features, "BASELINE_REGIME_NOT_TRENDING", total_cost)
    if values["relative_volume"] < 1.0:
        return _reject(symbol, as_of, raw_features, "BASELINE_RELATIVE_VOLUME_BELOW_ONE", total_cost)
    if live_spread_bps > 20.0:
        return _reject(symbol, as_of, raw_features, "BASELINE_SPREAD_TOO_WIDE", total_cost)
    if direction == Direction.LONG and (percentile < 0.75 or breadth < 0.45 or market_return <= 0):
        return _reject(symbol, as_of, raw_features, "BASELINE_LONG_REGIME_OR_RANK_GATE", total_cost)
    if direction == Direction.SHORT and (percentile > 0.25 or breadth > 0.55 or market_return >= 0):
        return _reject(symbol, as_of, raw_features, "BASELINE_SHORT_REGIME_OR_RANK_GATE", total_cost)

    gross_edge = abs(residual_score)
    expected_net = gross_edge - total_cost
    if expected_net <= 0:
        return _reject(symbol, as_of, raw_features, "BASELINE_COST_HURDLE_NOT_CLEARED", total_cost)

    confidence = max(0.5, min(0.95, 0.5 + abs(residual_score) / 200.0))
    opinion = ModelOpinion.create(
        model_id=BASELINE_MODEL_ID,
        model_version=BASELINE_MODEL_VERSION,
        symbol=symbol,
        direction=direction,
        as_of=as_of,
        horizon_minutes=BASELINE_HORIZON_MINUTES,
        probability=confidence,
        expected_return_bps=gross_edge,
        evidence_ids=(),
    )
    opportunity = Opportunity.create(
        symbol=symbol,
        direction=direction,
        as_of=as_of,
        expected_net_return_bps=expected_net,
        confidence=confidence,
        status=DecisionStatus.QUALIFIED,
        reason="BASELINE_RELATIVE_STRENGTH_COST_AWARE",
        opinion_ids=(opinion.opinion_id,),
    )
    return BaselineDecision(opportunity, (opinion,), dict(raw_features), total_cost)


def _reject(symbol, as_of, features, reason: str, cost_bps: float) -> BaselineDecision:
    opportunity = Opportunity.create(
        symbol=symbol,
        direction=Direction.FLAT,
        as_of=as_of,
        expected_net_return_bps=0.0,
        confidence=0.0,
        status=DecisionStatus.REJECTED,
        reason=reason,
        opinion_ids=(),
    )
    return BaselineDecision(opportunity, (), dict(features), cost_bps)