from __future__ import annotations

import tempfile
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.validation.metrics import PredictionOutcome, evaluate_predictions
from daybagger.validation.registry import ModelRegistry


def main() -> int:
    verify_golden_rules(REPO_ROOT)

    # Synthetic values here are ONLY a code-path self-test, never trading evidence.
    metrics = evaluate_predictions(
        [
            PredictionOutcome(0.70, 20),
            PredictionOutcome(0.40, -10),
            PredictionOutcome(0.65, 15),
        ]
    )

    with tempfile.TemporaryDirectory() as td:
        registry = ModelRegistry(Path(td) / "registry.json")
        registry.record(
            model_id="SELF_TEST_ONLY",
            version="0",
            validation_id="CODE_PATH_TEST",
            metrics=metrics,
            approved=True,
            reason="software self-check only; not a production model",
        )

    print("DAYBAGGER VALIDATION CHECK: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
