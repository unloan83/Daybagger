from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from daybagger.decision.ensemble import EvidenceWeightedEnsemble
from daybagger.decision.learning import ModelLearningStore
from daybagger.decision.model import ValidatedLinearModel, ValidatedModelSpec
from daybagger.decision.replay import DecisionSnapshot
from daybagger.decision.risk import AdaptiveCapitalAllocator, CapitalState
from daybagger.domain import DecisionStatus, Direction, ModelOpinion


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def test_validated_model_requires_features_and_validation_id() -> None:
    spec = ValidatedModelSpec(
        model_id="rs_trend",
        version="1",
        direction=Direction.LONG,
        horizon_minutes=30,
        feature_coefficients={"rs_bps": 0.01, "rvol": 0.2},
        bias=-0.3,
        favourable_move_bps=80,
        adverse_move_bps=45,
        validation_id="india-oos-001",
    )
    model = ValidatedLinearModel(spec)
    opinion = model.evaluate(
        symbol="AAA",
        as_of=NOW,
        features={"rs_bps": 40.0, "rvol": 1.5},
        evidence_ids=[uuid4()],
    )
    assert opinion is not None
    assert 0 <= opinion.probability <= 1


def test_ensemble_requires_positive_net_edge_after_costs() -> None:
    opinion = ModelOpinion.create(
        model_id="m1",
        model_version="1",
        symbol="AAA",
        direction=Direction.LONG,
        as_of=NOW,
        horizon_minutes=30,
        probability=0.7,
        expected_return_bps=25,
        evidence_ids=[],
    )
    ensemble = EvidenceWeightedEnsemble()

    yes = ensemble.rank(
        symbol="AAA",
        as_of=NOW,
        opinions=[opinion],
        model_weights={"m1": 1.0},
        estimated_round_trip_cost_bps=10,
    )
    no = ensemble.rank(
        symbol="AAA",
        as_of=NOW,
        opinions=[opinion],
        model_weights={"m1": 1.0},
        estimated_round_trip_cost_bps=30,
    )
    assert yes.status == DecisionStatus.QUALIFIED
    assert no.status == DecisionStatus.REJECTED


def test_drawdown_never_increases_risk() -> None:
    op = __import__("daybagger.domain", fromlist=["Opportunity"]).Opportunity.create(
        symbol="AAA",
        direction=Direction.LONG,
        as_of=NOW,
        expected_net_return_bps=60,
        confidence=0.8,
        status=DecisionStatus.QUALIFIED,
        reason="test",
        opinion_ids=[],
    )
    allocator = AdaptiveCapitalAllocator(
        base_risk_fraction=0.01,
        max_risk_fraction=0.02,
        max_position_fraction=0.5,
    )
    fresh = allocator.allocate(
        opportunity=op,
        capital=CapitalState(
            equity_inr=Decimal("30000"),
            available_cash_inr=Decimal("30000"),
            peak_equity_inr=Decimal("30000"),
        ),
        estimated_volatility_bps=100,
    )
    drawdown = allocator.allocate(
        opportunity=op,
        capital=CapitalState(
            equity_inr=Decimal("27000"),
            available_cash_inr=Decimal("27000"),
            peak_equity_inr=Decimal("30000"),
        ),
        estimated_volatility_bps=100,
    )
    assert drawdown.risk_fraction <= fresh.risk_fraction


def test_learning_uses_real_outcomes_and_zeroes_negative_expectancy(tmp_path: Path) -> None:
    store = ModelLearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    store.record(
        model_id="good",
        probability=0.7,
        predicted_return_bps=30,
        realised_net_return_bps=20,
        favourable_outcome=True,
    )
    store.record(
        model_id="bad",
        probability=0.8,
        predicted_return_bps=30,
        realised_net_return_bps=-20,
        favourable_outcome=False,
    )
    weights = store.weights()
    assert weights["good"] > 0
    assert weights["bad"] == 0


def test_replay_hash_is_deterministic() -> None:
    a = DecisionSnapshot(
        as_of_iso="2026-09-02T10:00:00+05:30",
        symbol="AAA",
        feature_payload={"b": 2, "a": 1},
        model_versions={"m": "1"},
        decision_payload={"status": "QUALIFIED"},
    )
    b = DecisionSnapshot(
        as_of_iso="2026-09-02T10:00:00+05:30",
        symbol="AAA",
        feature_payload={"a": 1, "b": 2},
        model_versions={"m": "1"},
        decision_payload={"status": "QUALIFIED"},
    )
    assert a.stable_hash() == b.stable_hash()
