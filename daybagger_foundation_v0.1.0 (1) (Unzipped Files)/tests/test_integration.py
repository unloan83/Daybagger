from datetime import datetime, timezone
from decimal import Decimal

from daybagger.decision.model import ValidatedModelSpec
from daybagger.decision.risk import AdaptiveCapitalAllocator, CapitalState
from daybagger.domain import Direction
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.integration.engine import CanonicalDecisionEngine


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def test_cost_model_is_nonzero_and_includes_indian_intraday_charges():
    model = IndiaEquityIntradayCostModel()
    costs = model.estimate_round_trip(
        buy_turnover=Decimal("10000"),
        sell_turnover=Decimal("10100"),
    )
    assert costs.total > 0
    assert costs.stt > 0
    assert costs.stamp > 0
    assert costs.gst > 0


def test_canonical_engine_skips_specialist_with_missing_evidence_without_defaults():
    good = ValidatedModelSpec(
        model_id="good",
        version="1",
        direction=Direction.LONG,
        horizon_minutes=30,
        feature_coefficients={"x": 0.1},
        bias=0.0,
        favourable_move_bps=100,
        adverse_move_bps=40,
        validation_id="oos-good",
    )
    missing = ValidatedModelSpec(
        model_id="missing",
        version="1",
        direction=Direction.LONG,
        horizon_minutes=30,
        feature_coefficients={"unknown": 0.1},
        bias=0.0,
        favourable_move_bps=100,
        adverse_move_bps=40,
        validation_id="oos-missing",
    )
    engine = CanonicalDecisionEngine(
        specs=[good, missing],
        model_weights={"good": 1.0, "missing": 1.0},
        allocator=AdaptiveCapitalAllocator(
            base_risk_fraction=0.01,
            max_risk_fraction=0.02,
            max_position_fraction=0.5,
        ),
    )
    trace = engine.decide(
        symbol="AAA",
        as_of=NOW,
        features={"x": 10.0},
        evidence_ids=[],
        capital=CapitalState(
            equity_inr=Decimal("30000"),
            available_cash_inr=Decimal("30000"),
            peak_equity_inr=Decimal("30000"),
        ),
        estimated_volatility_bps=100,
        reference_price=Decimal("100"),
        reference_quantity=10,
    )
    assert len(trace.opinions) == 1
    assert trace.opinions[0].model_id == "good"
