from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from daybagger.domain import DecisionStatus, Direction, ModelOpinion, Opportunity


BASELINE_MODEL_ID = "baseline_relative_strength"
BASELINE_VERSION = "baseline-v1"


@dataclass(frozen=True, slots=True)
class BaselineRegime:
    allowed: bool
    direction: Direction
    reason: str
    strength: float


@dataclass(frozen=True, slots=True)
class BaselineDecision:
    opportunity: Opportunity
    opinions: tuple[ModelOpinion, ...]
    estimated_total_cost_bps: float
    regime: BaselineRegime
    features_used: Mapping[str, float]
    gross_edge_bps: float
    residual_strength_bps: float


class RelativeStrengthBaselineDecider:
    """
    Deterministic paper-trading baseline.

    This is intentionally simple and explainable: trade only when market regime,
    relative strength, volume participation, and live execution cost line up.
    """

    def __init__(
        self,
        *,
        horizon_minutes: int = 15,
        paper_slippage_bps_per_side: float = 0.0,
        min_relative_volume: float = 1.1,
        min_market_trend_efficiency: float = 0.2,
        min_cross_section_dispersion_bps: float = 35.0,
        max_spread_share_of_range: float = 0.3,
    ) -> None:
        if horizon_minutes <= 0:
            raise ValueError("horizon_minutes must be positive")
        if paper_slippage_bps_per_side < 0:
            raise ValueError("paper_slippage_bps_per_side cannot be negative")
        if min_relative_volume <= 0:
            raise ValueError("min_relative_volume must be positive")
        if not 0 < min_market_trend_efficiency <= 1:
            raise ValueError("min_market_trend_efficiency must be in (0,1]")
        if min_cross_section_dispersion_bps <= 0:
            raise ValueError("min_cross_section_dispersion_bps must be positive")
        if not 0 < max_spread_share_of_range < 1:
            raise ValueError("max_spread_share_of_range must be in (0,1)")
        self.horizon_minutes = horizon_minutes
        self.paper_slippage_bps_per_side = paper_slippage_bps_per_side
        self.min_relative_volume = min_relative_volume
        self.min_market_trend_efficiency = min_market_trend_efficiency
        self.min_cross_section_dispersion_bps = min_cross_section_dispersion_bps
        self.max_spread_share_of_range = max_spread_share_of_range

    def decide(
        self,
        *,
        symbol: str,
        as_of,
        raw_features: Mapping[str, float],
        statutory_cost_bps: float,
        live_spread_bps: float,
    ) -> BaselineDecision:
        if statutory_cost_bps < 0 or live_spread_bps < 0:
            raise ValueError("costs cannot be negative")
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        required = (
            "rs_vs_benchmark_bps",
            "rs_vs_sector_bps",
            "relative_volume",
            "stock_session_range_bps",
            "stock_return_5m_bps",
            "stock_return_15m_bps",
            "market_return_15m_bps",
            "market_trend_efficiency",
            "bank_nifty_return_15m_bps",
            "breadth_advance_ratio",
            "cross_section_dispersion_bps",
        )
        missing = [name for name in required if name not in raw_features]
        if missing:
            raise ValueError(f"baseline decision missing features: {missing}")

        cost = statutory_cost_bps + live_spread_bps + 2.0 * self.paper_slippage_bps_per_side
        regime = self.regime(raw_features, cost_bps=cost)
        features_used = {
            name: float(raw_features[name])
            for name in required
        }

        residual = (
            0.6 * float(raw_features["rs_vs_benchmark_bps"])
            + 0.4 * float(raw_features["rs_vs_sector_bps"])
        )
        volume = float(raw_features["relative_volume"])
        session_range = float(raw_features["stock_session_range_bps"])
        spread_limit = max(6.0, session_range * self.max_spread_share_of_range)
        if live_spread_bps > spread_limit:
            return self._reject(
                symbol=symbol,
                as_of=as_of,
                direction=Direction.FLAT,
                cost_bps=cost,
                reason="SPREAD_TOO_WIDE_FOR_BASELINE_EDGE",
                confidence=0.0,
                features_used=features_used,
                residual_strength_bps=abs(residual),
                gross_edge_bps=0.0,
            )
        if volume < self.min_relative_volume:
            return self._reject(
                symbol=symbol,
                as_of=as_of,
                direction=Direction.FLAT,
                cost_bps=cost,
                reason="RELATIVE_VOLUME_TOO_LOW",
                confidence=0.0,
                features_used=features_used,
                residual_strength_bps=abs(residual),
                gross_edge_bps=0.0,
            )
        if not regime.allowed:
            return self._reject(
                symbol=symbol,
                as_of=as_of,
                direction=Direction.FLAT,
                cost_bps=cost,
                reason=regime.reason,
                confidence=0.0,
                features_used=features_used,
                residual_strength_bps=abs(residual),
                gross_edge_bps=0.0,
                regime=regime,
            )

        direction = regime.direction
        signed_residual = residual if direction == Direction.LONG else -residual
        if signed_residual <= 0:
            return self._reject(
                symbol=symbol,
                as_of=as_of,
                direction=direction,
                cost_bps=cost,
                reason="RESIDUAL_STRENGTH_NOT_ALIGNED_WITH_REGIME",
                confidence=0.0,
                features_used=features_used,
                residual_strength_bps=abs(residual),
                gross_edge_bps=0.0,
                regime=regime,
            )

        short_term_move = float(raw_features["stock_return_5m_bps"]) + 0.5 * float(
            raw_features["stock_return_15m_bps"]
        )
        aligned_short_term = (
            max(0.0, short_term_move) if direction == Direction.LONG else max(0.0, -short_term_move)
        )
        gross_edge = min(
            session_range * 0.55,
            signed_residual * 0.45
            + aligned_short_term * 0.2
            + max(0.0, volume - 1.0) * 12.0
            + regime.strength * 10.0,
        )
        net_edge = gross_edge - cost
        confidence = max(
            0.0,
            min(
                1.0,
                0.25
                + regime.strength * 0.3
                + min(1.0, signed_residual / max(cost, 10.0)) * 0.3
                + min(1.0, max(0.0, volume - 1.0)) * 0.15,
            ),
        )
        opinion = ModelOpinion.create(
            model_id=BASELINE_MODEL_ID,
            model_version=BASELINE_VERSION,
            symbol=symbol,
            direction=direction,
            as_of=as_of,
            horizon_minutes=self.horizon_minutes,
            probability=confidence,
            expected_return_bps=float(gross_edge),
            evidence_ids=(),
        )
        status = DecisionStatus.QUALIFIED if net_edge > 0 else DecisionStatus.REJECTED
        opportunity = Opportunity.create(
            symbol=symbol,
            direction=direction,
            as_of=as_of,
            expected_net_return_bps=float(net_edge),
            confidence=confidence,
            status=status,
            reason=(
                "BASELINE_RELATIVE_STRENGTH_EDGE_COVERS_LIVE_COST"
                if status == DecisionStatus.QUALIFIED
                else "BASELINE_RELATIVE_STRENGTH_EDGE_BELOW_LIVE_COST"
            ),
            opinion_ids=[opinion.opinion_id],
        )
        return BaselineDecision(
            opportunity=opportunity,
            opinions=(opinion,),
            estimated_total_cost_bps=cost,
            regime=regime,
            features_used=features_used,
            gross_edge_bps=float(gross_edge),
            residual_strength_bps=float(abs(residual)),
        )

    def regime(
        self,
        raw_features: Mapping[str, float],
        *,
        cost_bps: float,
    ) -> BaselineRegime:
        trend = float(raw_features["market_trend_efficiency"])
        breadth = float(raw_features["breadth_advance_ratio"])
        market_15m = float(raw_features["market_return_15m_bps"])
        bank_15m = float(raw_features["bank_nifty_return_15m_bps"])
        dispersion = float(raw_features["cross_section_dispersion_bps"])

        if trend < self.min_market_trend_efficiency:
            return BaselineRegime(False, Direction.FLAT, "REGIME_LOW_MARKET_TREND_EFFICIENCY", 0.0)
        if dispersion < max(self.min_cross_section_dispersion_bps, cost_bps):
            return BaselineRegime(False, Direction.FLAT, "REGIME_LOW_CROSS_SECTION_DISPERSION", 0.0)
        if abs(market_15m) < max(8.0, cost_bps * 0.25):
            return BaselineRegime(False, Direction.FLAT, "REGIME_LOW_BENCHMARK_IMPULSE", 0.0)

        if market_15m > 0 and breadth >= 0.52 and bank_15m >= -10.0:
            strength = min(
                1.0,
                0.5 * trend
                + 0.25 * min(1.0, (breadth - 0.5) / 0.2)
                + 0.25 * min(1.0, market_15m / max(cost_bps, 20.0)),
            )
            return BaselineRegime(True, Direction.LONG, "LONG_RELATIVE_STRENGTH_REGIME", strength)
        if market_15m < 0 and breadth <= 0.48 and bank_15m <= 10.0:
            strength = min(
                1.0,
                0.5 * trend
                + 0.25 * min(1.0, (0.5 - breadth) / 0.2)
                + 0.25 * min(1.0, abs(market_15m) / max(cost_bps, 20.0)),
            )
            return BaselineRegime(True, Direction.SHORT, "SHORT_RELATIVE_STRENGTH_REGIME", strength)
        return BaselineRegime(False, Direction.FLAT, "REGIME_DIRECTION_UNCLEAR", 0.0)

    def _reject(
        self,
        *,
        symbol: str,
        as_of,
        direction: Direction,
        cost_bps: float,
        reason: str,
        confidence: float,
        features_used: Mapping[str, float],
        residual_strength_bps: float,
        gross_edge_bps: float,
        regime: BaselineRegime | None = None,
    ) -> BaselineDecision:
        regime = regime or BaselineRegime(False, Direction.FLAT, reason, 0.0)
        opportunity = Opportunity.create(
            symbol=symbol,
            direction=direction,
            as_of=as_of,
            expected_net_return_bps=float(gross_edge_bps - cost_bps),
            confidence=confidence,
            status=DecisionStatus.REJECTED,
            reason=reason,
            opinion_ids=[],
        )
        return BaselineDecision(
            opportunity=opportunity,
            opinions=(),
            estimated_total_cost_bps=cost_bps,
            regime=regime,
            features_used=features_used,
            gross_edge_bps=float(gross_edge_bps),
            residual_strength_bps=float(residual_strength_bps),
        )
