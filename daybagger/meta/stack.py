from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from daybagger.decision.model import ValidatedLinearModel, ValidatedModelSpec
from daybagger.domain import DecisionStatus, Direction, ModelOpinion, Opportunity
from daybagger.meta.forest import ForestRegressorSpec


RUNTIME_META_VERSION = "meta-v4-direct-return"
BASE_FAMILIES = ("relative_strength", "trend_pullback", "volume_participation")
META_CONTEXT_FEATURES = (
    "stock_session_return_bps",
    "stock_return_5m_bps",
    "stock_return_15m_bps",
    "stock_return_30m_bps",
    "stock_vwap_distance_bps",
    "stock_trend_efficiency",
    "stock_close_location",
    "stock_session_range_bps",
    "relative_volume",
    "rs_vs_benchmark_bps",
    "rs_vs_sector_bps",
    "sector_session_return_percentile",
    "cross_section_return_percentile",
    "cross_section_dispersion_bps",
    "breadth_advance_ratio",
    "breadth_median_return_bps",
    "market_session_return_bps",
    "market_return_15m_bps",
    "market_trend_efficiency",
    "market_session_range_bps",
    "bank_nifty_session_return_bps",
    "bank_nifty_return_15m_bps",
    "bank_nifty_trend_efficiency",
    "bank_nifty_session_range_bps",
    "india_vix_session_return_bps",
    "india_vix_return_15m_bps",
    "india_vix_session_range_bps",
)
OPTIONAL_META_PREFIXES = (
    "fii_",
    "dii_",
    "gift_nifty_",
    "brent_",
    "usd_inr_",
)


@dataclass(frozen=True, slots=True)
class MetaIntelligenceSpec:
    validation_id: str
    version: str
    horizon_minutes: int
    base_specs: tuple[ValidatedModelSpec, ...]
    long_model: ForestRegressorSpec
    short_model: ForestRegressorSpec
    meta_feature_names: tuple[str, ...]
    evidence_summary: Mapping[str, object]

    def validate(self) -> None:
        if not self.validation_id.strip() or not self.version.strip():
            raise ValueError("meta validation_id/version required")
        if self.horizon_minutes <= 0:
            raise ValueError("meta horizon must be positive")
        if not self.base_specs:
            raise ValueError("meta model requires validated base specialist specs")
        for spec in self.base_specs:
            spec.validate()
        self.long_model.validate()
        self.short_model.validate()
        if self.long_model.horizon_minutes != self.horizon_minutes:
            raise ValueError("LONG meta horizon mismatch")
        if self.short_model.horizon_minutes != self.horizon_minutes:
            raise ValueError("SHORT meta horizon mismatch")
        if tuple(self.long_model.feature_names) != tuple(self.meta_feature_names):
            raise ValueError("LONG meta features mismatch")
        if tuple(self.short_model.feature_names) != tuple(self.meta_feature_names):
            raise ValueError("SHORT meta features mismatch")

    def to_dict(self) -> dict:
        self.validate()
        return {
            "approved": True,
            "family_id": "meta_intelligence",
            "validation_id": self.validation_id,
            "version": self.version,
            "horizon_minutes": self.horizon_minutes,
            "meta_feature_names": list(self.meta_feature_names),
            "base_specs": [_linear_spec_to_dict(spec) for spec in self.base_specs],
            "long_model": self.long_model.to_dict(),
            "short_model": self.short_model.to_dict(),
            "evidence_summary": dict(self.evidence_summary),
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "MetaIntelligenceSpec":
        if not payload.get("approved", False):
            raise ValueError("meta model is not approved")
        spec = cls(
            validation_id=str(payload["validation_id"]),
            version=str(payload["version"]),
            horizon_minutes=int(payload["horizon_minutes"]),
            base_specs=tuple(_linear_spec_from_dict(item) for item in payload["base_specs"]),
            long_model=ForestRegressorSpec.from_dict(payload["long_model"]),
            short_model=ForestRegressorSpec.from_dict(payload["short_model"]),
            meta_feature_names=tuple(str(v) for v in payload["meta_feature_names"]),
            evidence_summary=dict(payload.get("evidence_summary") or {}),
        )
        spec.validate()
        return spec


@dataclass(frozen=True, slots=True)
class MetaDecision:
    opportunity: Opportunity
    opinions: tuple[ModelOpinion, ...]
    long_probability: float
    short_probability: float
    long_expected_gross_bps: float
    short_expected_gross_bps: float
    estimated_total_cost_bps: float
    meta_features: Mapping[str, float]


def build_meta_features(
    *,
    symbol: str,
    as_of: datetime,
    raw_features: Mapping[str, float],
    base_specs: Sequence[ValidatedModelSpec],
) -> dict[str, float]:
    result: dict[str, float] = {}
    for opinion in _base_opinions(
        symbol=symbol,
        as_of=as_of,
        raw_features=raw_features,
        base_specs=base_specs,
    ):
        prefix = f"base_{opinion.model_id}"
        result[f"{prefix}_probability"] = float(opinion.probability)
        result[f"{prefix}_expected_gross_bps"] = float(opinion.expected_return_bps)

    for name in META_CONTEXT_FEATURES:
        if name not in raw_features:
            raise ValueError(f"missing required meta context feature: {name}")
        result[name] = float(raw_features[name])
    for name, value in raw_features.items():
        if name.startswith(OPTIONAL_META_PREFIXES):
            result[name] = float(value)
    return result


def choose_meta_feature_names(rows: Sequence[Mapping[str, float]]) -> tuple[str, ...]:
    if not rows:
        raise ValueError("meta rows cannot be empty")
    common = set(rows[0])
    for row in rows[1:]:
        common.intersection_update(row)
    required = set(META_CONTEXT_FEATURES)
    base = {name for name in common if name.startswith("base_")}
    optional = {
        name for name in common
        if name.startswith(OPTIONAL_META_PREFIXES)
    }
    names = sorted(required | base | optional)
    missing = [name for name in required if name not in common]
    if missing:
        raise ValueError(f"meta training rows missing required context features: {missing}")
    return tuple(names)


def decide_meta(
    *,
    spec: MetaIntelligenceSpec,
    symbol: str,
    as_of: datetime,
    raw_features: Mapping[str, float],
    statutory_cost_bps: float,
    live_spread_bps: float,
    paper_slippage_bps_per_side: float = 0.0,
) -> MetaDecision:
    if statutory_cost_bps < 0 or live_spread_bps < 0 or paper_slippage_bps_per_side < 0:
        raise ValueError("costs cannot be negative")
    meta_features = build_meta_features(
        symbol=symbol,
        as_of=as_of,
        raw_features=raw_features,
        base_specs=spec.base_specs,
    )
    missing = [name for name in spec.meta_feature_names if name not in meta_features]
    if missing:
        raise ValueError(f"live intelligence missing validated meta features: {missing}")
    selected = {name: meta_features[name] for name in spec.meta_feature_names}
    long_gross = spec.long_model.expected_gross_return_bps(selected)
    short_gross = spec.short_model.expected_gross_return_bps(selected)
    # Live spread approximates the round-trip bid/ask crossing cost. Paper
    # slippage is charged independently on entry and exit.
    cost = statutory_cost_bps + live_spread_bps + 2.0 * paper_slippage_bps_per_side
    # Direct-return forests expose the distribution of tree return forecasts.
    # Confidence is the fraction of trees whose forecast clears the CURRENT cost.
    long_p = spec.long_model.probability_above(selected, cost)
    short_p = spec.short_model.probability_above(selected, cost)
    base_opinions = _base_opinions(
        symbol=symbol,
        as_of=as_of,
        raw_features=raw_features,
        base_specs=spec.base_specs,
    )
    meta_long = ModelOpinion.create(
        model_id=spec.long_model.model_id,
        model_version=spec.long_model.version,
        symbol=symbol,
        direction=Direction.LONG,
        as_of=as_of,
        horizon_minutes=spec.horizon_minutes,
        probability=long_p,
        expected_return_bps=long_gross,
        evidence_ids=(),
    )
    meta_short = ModelOpinion.create(
        model_id=spec.short_model.model_id,
        model_version=spec.short_model.version,
        symbol=symbol,
        direction=Direction.SHORT,
        as_of=as_of,
        horizon_minutes=spec.horizon_minutes,
        probability=short_p,
        expected_return_bps=short_gross,
        evidence_ids=(),
    )
    opinions = (*base_opinions, meta_long, meta_short)
    candidates = [
        (Direction.LONG, long_gross - cost, long_p),
        (Direction.SHORT, short_gross - cost, short_p),
    ]
    candidates.sort(key=lambda item: item[1], reverse=True)
    direction, net, confidence = candidates[0]
    status = DecisionStatus.QUALIFIED if net > 0 else DecisionStatus.REJECTED
    opportunity = Opportunity.create(
        symbol=symbol,
        direction=direction,
        as_of=as_of,
        expected_net_return_bps=float(net),
        confidence=float(confidence),
        status=status,
        reason=(
            "DIRECT_RETURN_EDGE_COVERS_STATUTORY_AND_LIVE_SPREAD_COST"
            if status == DecisionStatus.QUALIFIED
            else "DIRECT_RETURN_EDGE_DOES_NOT_COVER_LIVE_COST"
        ),
        opinion_ids=[op.opinion_id for op in opinions],
    )
    return MetaDecision(
        opportunity=opportunity,
        opinions=tuple(opinions),
        long_probability=long_p,
        short_probability=short_p,
        long_expected_gross_bps=long_gross,
        short_expected_gross_bps=short_gross,
        estimated_total_cost_bps=cost,
        meta_features=selected,
    )


def _base_opinions(
    *,
    symbol: str,
    as_of: datetime,
    raw_features: Mapping[str, float],
    base_specs: Sequence[ValidatedModelSpec],
) -> tuple[ModelOpinion, ...]:
    result: list[ModelOpinion] = []
    for spec in base_specs:
        opinion = ValidatedLinearModel(spec).evaluate(
            symbol=symbol,
            as_of=as_of,
            features=raw_features,
            evidence_ids=(),
        )
        if opinion is None:
            raise ValueError(f"base specialist disabled: {spec.model_id}")
        result.append(opinion)
    return tuple(result)


def load_meta_spec(path: Path) -> MetaIntelligenceSpec | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("approved", False):
        return None
    spec = MetaIntelligenceSpec.from_dict(payload)
    if spec.version != RUNTIME_META_VERSION:
        return None
    return spec


def promote_meta_spec(path: Path, spec: MetaIntelligenceSpec) -> None:
    if spec.version != RUNTIME_META_VERSION:
        raise ValueError(f"unsupported production meta version: {spec.version}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(spec.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _linear_spec_to_dict(spec: ValidatedModelSpec) -> dict:
    return {
        "model_id": spec.model_id,
        "version": spec.version,
        "direction": spec.direction.value,
        "horizon_minutes": spec.horizon_minutes,
        "feature_coefficients": dict(spec.feature_coefficients),
        "bias": spec.bias,
        "favourable_move_bps": spec.favourable_move_bps,
        "adverse_move_bps": spec.adverse_move_bps,
        "validation_id": spec.validation_id,
        "enabled": spec.enabled,
    }


def _linear_spec_from_dict(payload: Mapping) -> ValidatedModelSpec:
    spec = ValidatedModelSpec(
        model_id=str(payload["model_id"]),
        version=str(payload["version"]),
        direction=Direction(str(payload["direction"])),
        horizon_minutes=int(payload["horizon_minutes"]),
        feature_coefficients={str(k): float(v) for k, v in payload["feature_coefficients"].items()},
        bias=float(payload["bias"]),
        favourable_move_bps=float(payload["favourable_move_bps"]),
        adverse_move_bps=float(payload["adverse_move_bps"]),
        validation_id=str(payload["validation_id"]),
        enabled=bool(payload.get("enabled", True)),
    )
    spec.validate()
    return spec
