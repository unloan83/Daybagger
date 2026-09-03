from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from daybagger.validation.metrics import ValidationMetrics


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    model_id: str
    version: str
    validation_id: str
    metrics: ValidationMetrics
    approved: bool
    reason: str
    created_at_utc: str


class ModelRegistry:
    """
    File-based, free-tier-friendly registry.

    Promotion criteria are supplied explicitly by the validation run.
    The registry itself contains no magic win-rate/Sharpe thresholds.
    """

    def __init__(self, path: Path):
        self.path = path

    def record(
        self,
        *,
        model_id: str,
        version: str,
        validation_id: str,
        metrics: ValidationMetrics,
        approved: bool,
        reason: str,
    ) -> PromotionRecord:
        if not model_id.strip() or not version.strip() or not validation_id.strip():
            raise ValueError("model_id/version/validation_id are required")
        if approved and metrics.avg_net_return_bps <= 0:
            raise ValueError(
                "cannot approve a model with non-positive out-of-sample net expectancy"
            )

        record = PromotionRecord(
            model_id=model_id,
            version=version,
            validation_id=validation_id,
            metrics=metrics,
            approved=approved,
            reason=reason,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = []
        if self.path.exists():
            current = json.loads(self.path.read_text(encoding="utf-8"))
        current.append(
            {
                **asdict(record),
                "metrics": asdict(record.metrics),
            }
        )
        self.path.write_text(
            json.dumps(current, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return record
