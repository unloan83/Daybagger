from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median
from typing import Mapping, Sequence

from daybagger.domain import Direction
from daybagger.specialists.catalog import SPECIALIST_FAMILIES


@dataclass(frozen=True, slots=True)
class TrainingRow:
    features: Mapping[str, float]
    favourable_outcome: bool
    realised_net_return_bps: float


def fit_logistic_specialist(
    *,
    family_id: str,
    model_id: str,
    version: str,
    direction: Direction,
    horizon_minutes: int,
    validation_id: str,
    rows: Sequence[TrainingRow],
    C: float = 1.0,
) -> dict:
    """
    OFFLINE research utility using scikit-learn.

    Live OCI does not need scikit-learn; this exports raw coefficients consumed by
    Daybagger's lightweight standard-library inference engine.
    """
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import make_pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Offline training requires scikit-learn and numpy. "
            "Install from research/requirements.txt; live runtime does not need them."
        ) from exc

    family = SPECIALIST_FAMILIES.get(family_id)
    if family is None:
        raise ValueError(f"unknown family_id: {family_id}")
    if len(rows) < 2:
        raise ValueError("at least two training observations are required")
    if C <= 0:
        raise ValueError("C must be > 0")

    names = list(family.required_features)
    X = []
    y = []
    realised = []
    for row in rows:
        missing = [name for name in names if name not in row.features]
        if missing:
            raise ValueError(f"training row missing features: {missing}")
        X.append([float(row.features[name]) for name in names])
        y.append(1 if row.favourable_outcome else 0)
        realised.append(float(row.realised_net_return_bps))

    if len(set(y)) < 2:
        raise ValueError("training data must contain both favourable and unfavourable outcomes")

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    pipe = make_pipeline(
        StandardScaler(),
        LogisticRegression(C=C, max_iter=2000, solver="lbfgs"),
    )
    pipe.fit(X, y)

    scaler = pipe.named_steps["standardscaler"]
    model = pipe.named_steps["logisticregression"]
    scaled_coef = model.coef_[0]
    raw_coef = scaled_coef / scaler.scale_
    raw_bias = float(model.intercept_[0] - (scaled_coef * scaler.mean_ / scaler.scale_).sum())

    positives = [r for r in realised if r > 0]
    negatives = [abs(r) for r in realised if r < 0]
    if not positives or not negatives:
        raise ValueError("realised outcomes must include positive and negative net returns")

    return {
        "family_id": family_id,
        "model_id": model_id,
        "version": version,
        "direction": direction.value,
        "horizon_minutes": horizon_minutes,
        "feature_coefficients": {
            name: float(coef) for name, coef in zip(names, raw_coef)
        },
        "bias": raw_bias,
        "favourable_move_bps": float(median(positives)),
        "adverse_move_bps": float(median(negatives)),
        "validation_id": validation_id,
        "approved": False,
        "training_note": "Must pass walk-forward/OOS validation before approved=true.",
    }
