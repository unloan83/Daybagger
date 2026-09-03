from __future__ import annotations

import json
from pathlib import Path

from daybagger.decision.model import ValidatedModelSpec
from daybagger.domain import Direction
from daybagger.specialists.catalog import SPECIALIST_FAMILIES


class SpecialistLoadError(RuntimeError):
    """Validated specialist specification cannot be loaded."""


def load_validated_model_specs(path: Path) -> list[ValidatedModelSpec]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SpecialistLoadError("validated model file must contain a JSON list")

    result: list[ValidatedModelSpec] = []
    for item in payload:
        if not item.get("approved", False):
            continue
        family_id = str(item.get("family_id", ""))
        family = SPECIALIST_FAMILIES.get(family_id)
        if family is None:
            raise SpecialistLoadError(f"unknown specialist family: {family_id}")

        coeffs = {str(k): float(v) for k, v in item["feature_coefficients"].items()}
        missing = [name for name in family.required_features if name not in coeffs]
        if missing:
            raise SpecialistLoadError(
                f"{item.get('model_id')}: validated spec omits family features: {missing}"
            )

        spec = ValidatedModelSpec(
            model_id=str(item["model_id"]),
            version=str(item["version"]),
            direction=Direction(str(item["direction"])),
            horizon_minutes=int(item["horizon_minutes"]),
            feature_coefficients=coeffs,
            bias=float(item["bias"]),
            favourable_move_bps=float(item["favourable_move_bps"]),
            adverse_move_bps=float(item["adverse_move_bps"]),
            validation_id=str(item["validation_id"]),
            enabled=True,
        )
        spec.validate()
        result.append(spec)
    return result
