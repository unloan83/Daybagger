from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from daybagger.data.upstox import IntradayCandle
from daybagger.decision.learning import ModelLearningStore
from daybagger.domain import Direction, ModelOpinion
from daybagger.operations.outcomes import OutcomeLearner
from daybagger.operations.readiness import run_readiness


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def test_outcome_learning_uses_future_observed_candles(tmp_path: Path):
    store = ModelLearningStore(tmp_path / "learn.sqlite3")
    store.initialize()
    learner = OutcomeLearner(store)
    opinion = ModelOpinion.create(
        model_id="m1",
        model_version="1",
        symbol="AAA",
        direction=Direction.LONG,
        as_of=NOW,
        horizon_minutes=2,
        probability=0.7,
        expected_return_bps=30,
        evidence_ids=[],
    )
    future = [
        IntradayCandle("NSE_EQ|AAA", NOW + timedelta(minutes=1), Decimal("100"), Decimal("101"), Decimal("99.8"), Decimal("100.5"), 100, 0),
        IntradayCandle("NSE_EQ|AAA", NOW + timedelta(minutes=2), Decimal("100.5"), Decimal("102"), Decimal("100.4"), Decimal("101.5"), 120, 0),
    ]
    outcomes = learner.record_horizon_outcomes(
        opinions=[opinion],
        entry_reference_price=Decimal("100"),
        future_candles=future,
        estimated_cost_bps=10,
    )
    assert outcomes[0].realised_net_return_bps > 0
    assert store.weights()["m1"] > 0


def test_readiness_fails_closed_without_models_or_token(tmp_path: Path):
    (tmp_path / "goldenrules.txt").write_text("rules", encoding="utf-8")
    report = run_readiness(
        repo_root=tmp_path,
        specs=[],
        access_token_present=False,
    )
    assert report.ready is False
    assert "UPSTOX_TOKEN_MISSING" in report.failures
    assert "NO_APPROVED_VALIDATED_MODELS" in report.failures
