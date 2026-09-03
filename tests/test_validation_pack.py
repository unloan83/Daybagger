from datetime import date
from pathlib import Path

import pytest

from daybagger.validation.metrics import PredictionOutcome, evaluate_predictions
from daybagger.validation.registry import ModelRegistry
from daybagger.validation.walkforward import DatedItem, chronological_folds


def test_metrics_include_cost_adjusted_expectancy_drawdown_and_calibration() -> None:
    metrics = evaluate_predictions(
        [
            PredictionOutcome(0.8, 25),
            PredictionOutcome(0.7, 10),
            PredictionOutcome(0.4, -15),
            PredictionOutcome(0.6, 5),
        ]
    )
    assert metrics.observations == 4
    assert metrics.avg_net_return_bps > 0
    assert metrics.max_drawdown_bps == pytest.approx(15)
    assert 0 <= metrics.brier_score <= 1


def test_walkforward_never_shuffles_future_into_training() -> None:
    items = [
        DatedItem(date(2026, 1, day), day)
        for day in range(1, 13)
    ]
    folds = chronological_folds(
        items,
        train_sessions=5,
        validation_sessions=2,
        test_sessions=2,
        step_sessions=2,
    )
    assert folds
    for fold in folds:
        assert max(x.session_date for x in fold.train) < min(
            x.session_date for x in fold.validation
        )
        assert max(x.session_date for x in fold.validation) < min(
            x.session_date for x in fold.test
        )


def test_registry_rejects_approval_of_negative_expectancy(tmp_path: Path) -> None:
    metrics = evaluate_predictions(
        [
            PredictionOutcome(0.8, -20),
            PredictionOutcome(0.7, -10),
        ]
    )
    registry = ModelRegistry(tmp_path / "registry.json")
    with pytest.raises(ValueError):
        registry.record(
            model_id="bad",
            version="1",
            validation_id="oos-1",
            metrics=metrics,
            approved=True,
            reason="should fail",
        )
