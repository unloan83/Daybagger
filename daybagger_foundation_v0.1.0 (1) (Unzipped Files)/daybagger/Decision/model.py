from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp
from typing import Mapping, Sequence

from daybagger.domain import Direction, ModelOpinion


class DecisionModelError(RuntimeError):
    """A decision model cannot operate safely on the supplied evidence."""


@dataclass(frozen=True, slots=True)
class ValidatedModelSpec:
    """
    A specialist model whose coefficients come from offline Indian-market validation.

    Daybagger does not invent coefficients at runtime. A model must carry a non-empty
    validation_id so the runtime can trace the evidence that approved it.
    """
    model_id: str
    version: str
    direction: Direction
    horizon_minutes: int
    feature_coefficients: Mapping[str, float]
    bias: float
    favourable_move_bps: float
    adverse_move_bps: float
    validation_id: str
    enabled: bool = True

    def validate(self) -> None:
        if not self.model_id.strip() or not self.version.strip():
            raise DecisionModelError("model_id and version are required")
        if self.direction == Direction.FLAT:
            raise DecisionModelError("specialist direction cannot be FLAT")
        if self.horizon_minutes <= 0:
            raise DecisionModelError("horizon_minutes must be > 0")
        if not self.feature_coefficients:
            raise DecisionModelError("feature_coefficients cannot be empty")
        if self.favourable_move_bps <= 0 or self.adverse_move_bps <= 0:
            raise DecisionModelError("favourable/adverse move assumptions must be positive")
        if not self.validation_id.strip():
            raise DecisionModelError("validation_id is mandatory")


class ValidatedLinearModel:
    """
    Lightweight probabilistic specialist.

    Formula:
      p = sigmoid(bias + Σ coefficient_i * feature_i)
      expected_return_bps = p*favourable - (1-p)*adverse

    The formula is generic; the coefficients and move assumptions must come from
    validated historical/replay evidence, not hard-coded trader folklore.
    """

    def __init__(self, spec: ValidatedModelSpec):
        spec.validate()
        self.spec = spec

    def evaluate(
        self,
        *,
        symbol: str,
        as_of: datetime,
        features: Mapping[str, float],
        evidence_ids: Sequence,
    ) -> ModelOpinion | None:
        if not self.spec.enabled:
            return None
        if as_of.tzinfo is None:
            raise DecisionModelError("as_of must be timezone-aware")

        missing = [
            name for name in self.spec.feature_coefficients
            if name not in features
        ]
        if missing:
            raise DecisionModelError(
                f"{self.spec.model_id}: missing required features: {', '.join(missing)}"
            )

        z = self.spec.bias
        for name, coef in self.spec.feature_coefficients.items():
            value = float(features[name])
            if value != value or value in (float("inf"), float("-inf")):
                raise DecisionModelError(f"{self.spec.model_id}: invalid feature {name}")
            z += coef * value

        p = _sigmoid(z)
        expected = (
            p * self.spec.favourable_move_bps
            - (1.0 - p) * self.spec.adverse_move_bps
        )

        return ModelOpinion.create(
            model_id=self.spec.model_id,
            model_version=self.spec.version,
            symbol=symbol,
            direction=self.spec.direction,
            as_of=as_of,
            horizon_minutes=self.spec.horizon_minutes,
            probability=p,
            expected_return_bps=expected,
            evidence_ids=evidence_ids,
        )


def _sigmoid(value: float) -> float:
    # Stable enough for lightweight inference without external dependencies.
    if value >= 0:
        z = exp(-value)
        return 1.0 / (1.0 + z)
    z = exp(value)
    return z / (1.0 + z)
