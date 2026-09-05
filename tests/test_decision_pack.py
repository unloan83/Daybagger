from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

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
    # Stability guard: one lucky observation must never enter production.
    weights = store.weights()
    assert weights["good"] == 0
    assert weights["bad"] == 0

    for _ in range(25):
        store.record(
            model_id="good", probability=0.7, predicted_return_bps=30,
            realised_net_return_bps=20, favourable_outcome=True,
        )
    assert store.weights()["good"] > 0


def test_learning_veto_requires_sufficient_negative_evidence(tmp_path: Path) -> None:
    store = ModelLearningStore(
        tmp_path / "learning.sqlite3", minimum_observations=3
    )
    store.initialize()
    for _ in range(2):
        store.record(
            model_id="bad",
            probability=0.7,
            predicted_return_bps=30,
            realised_net_return_bps=-20,
            favourable_outcome=False,
        )
    assert store.vetoed_model_ids() == frozenset()
    store.record(
        model_id="bad",
        probability=0.7,
        predicted_return_bps=30,
        realised_net_return_bps=-20,
        favourable_outcome=False,
    )
    assert store.vetoed_model_ids() == frozenset({"bad"})


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


def test_allocator_reservation_and_hard_limits_are_portfolio_aware() -> None:
    Opportunity = __import__("daybagger.domain", fromlist=["Opportunity"]).Opportunity
    op = Opportunity.create(
        symbol="AAA",
        direction=Direction.LONG,
        as_of=NOW,
        expected_net_return_bps=80,
        confidence=0.9,
        status=DecisionStatus.QUALIFIED,
        reason="test",
        opinion_ids=[],
    )
    allocator = AdaptiveCapitalAllocator(
        base_risk_fraction=0.02,
        max_risk_fraction=0.02,
        max_position_fraction=0.5,
        hard_daily_loss_limit_inr=Decimal("1000"),
        max_aggregate_open_risk_inr=Decimal("700"),
    )
    capital = CapitalState(
        equity_inr=Decimal("30000"),
        available_cash_inr=Decimal("30000"),
        peak_equity_inr=Decimal("30000"),
        open_risk_inr=Decimal("0"),
        daily_net_pnl_inr=Decimal("0"),
    )
    first = allocator.allocate(opportunity=op, capital=capital, estimated_volatility_bps=100)
    assert first.approved
    reserved = capital.reserve(capital_inr=first.capital_inr, risk_inr=first.max_loss_inr)
    assert reserved.available_cash_inr == Decimal("15000.0")
    assert reserved.open_risk_inr == first.max_loss_inr

    blocked_loss = allocator.allocate(
        opportunity=op,
        capital=CapitalState(
            equity_inr=Decimal("29000"), available_cash_inr=Decimal("29000"),
            peak_equity_inr=Decimal("30000"), daily_net_pnl_inr=Decimal("-1000"),
        ),
        estimated_volatility_bps=100,
    )
    assert not blocked_loss.approved
    assert blocked_loss.reason == "DAILY_LOSS_LIMIT_REACHED"

    blocked_risk = allocator.allocate(
        opportunity=op,
        capital=CapitalState(
            equity_inr=Decimal("30000"), available_cash_inr=Decimal("15000"),
            peak_equity_inr=Decimal("30000"), open_risk_inr=Decimal("700"),
        ),
        estimated_volatility_bps=100,
    )
    assert not blocked_risk.approved
    assert blocked_risk.reason == "AGGREGATE_OPEN_RISK_LIMIT_REACHED"


def test_execution_sizer_returns_integer_quantity_and_rechecks_actual_costs() -> None:
    from daybagger.decision.risk import AllocationDecision, ExecutionSizer
    from daybagger.domain import ExecutableQuote, Opportunity

    op = Opportunity.create(
        symbol="AAA", direction=Direction.LONG, as_of=NOW,
        expected_net_return_bps=100, confidence=0.8,
        status=DecisionStatus.QUALIFIED, reason="test", opinion_ids=[],
    )
    allocation = AllocationDecision(
        approved=True, reason="test", capital_inr=Decimal("10000"),
        max_loss_inr=Decimal("300"), risk_fraction=0.01,
    )
    q = ExecutableQuote(
        symbol="AAA", as_of=NOW, bid=Decimal("99.90"),
        ask=Decimal("100.00"), last=Decimal("99.95"),
    )
    sized = ExecutionSizer().size(
        opportunity=op,
        allocation=allocation,
        quote=q,
        estimated_volatility_bps=100,
        slippage_bps=2,
    )
    assert sized.approved
    assert isinstance(sized.quantity, int) and sized.quantity > 0
    assert sized.entry_notional_inr <= allocation.capital_inr
    assert sized.estimated_adverse_loss_inr + sized.estimated_round_trip_cost_inr <= allocation.max_loss_inr


def test_small_sample_learning_uses_student_t_not_fixed_normal_bound() -> None:
    from daybagger.decision.learning import _student_t_critical_95

    assert _student_t_critical_95(19) > 1.96
    assert _student_t_critical_95(99) < _student_t_critical_95(19)
    assert _student_t_critical_95(9999) == pytest.approx(1.96, abs=0.001)
