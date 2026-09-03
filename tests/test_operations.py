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
    store = ModelLearningStore(tmp_path / "learn.sqlite3", minimum_observations=2)
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
    # A single outcome is deliberately insufficient for production weight.
    assert store.weights()["m1"] == 0


def test_readiness_fails_closed_without_models_or_token(tmp_path: Path):
    (tmp_path / "goldenrules.txt").write_text("rules", encoding="utf-8")
    report = run_readiness(
        repo_root=tmp_path,
        access_token_present=False,
    )
    assert report.ready is False
    assert "UPSTOX_TOKEN_MISSING" in report.failures
    assert "NO_APPROVED_VALIDATED_META_MODEL" in report.failures


def test_rejected_decision_retains_full_opinion_for_later_learning(tmp_path: Path):
    from daybagger.domain import DecisionStatus, Opportunity
    from daybagger.operations.trace_store import DecisionTraceStore

    trace = DecisionTraceStore(tmp_path / "traces.sqlite3")
    trace.initialize()
    opn = ModelOpinion.create(
        model_id="m_rejected", model_version="1", symbol="AAA",
        direction=Direction.LONG, as_of=NOW, horizon_minutes=5,
        probability=0.61, expected_return_bps=18.0, evidence_ids=[],
    )
    opportunity = Opportunity.create(
        symbol="AAA", direction=Direction.LONG, as_of=NOW,
        expected_net_return_bps=-2.0, confidence=0.61,
        status=DecisionStatus.REJECTED, reason="COST", opinion_ids=[opn.opinion_id],
    )
    trace_id = trace.record_decision(
        symbol="AAA", instrument_key="NSE_EQ|AAA", as_of=NOW,
        opportunity=opportunity, allocation_approved=False,
        estimated_cost_bps=20.0, opinions=[opn],
        validation_ids={"m_rejected": "val-1"}, reference_price=Decimal("100"),
        features={"x": 1.0},
    )
    pending = trace.pending_outcomes(ready_at=NOW + timedelta(minutes=5))
    assert len(pending) == 1
    assert pending[0].trace_id == trace_id
    restored = pending[0].opinions[0]
    assert restored.model_id == "m_rejected"
    assert restored.probability == 0.61
    assert restored.expected_return_bps == 18.0
