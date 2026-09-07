from datetime import datetime

from daybagger.decision.baseline import decide_baseline
from daybagger.domain import DecisionStatus, Direction


NOW = datetime(2026, 9, 2, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))


def features(**overrides):
    result = {
        "rs_vs_benchmark_bps": 35.0,
        "rs_vs_sector_bps": 25.0,
        "cross_section_return_percentile": 0.9,
        "relative_volume": 1.4,
        "market_trend_efficiency": 0.3,
        "market_session_return_bps": 40.0,
        "breadth_advance_ratio": 0.65,
        "stock_session_range_bps": 80.0,
    }
    result.update(overrides)
    return result


def test_baseline_qualifies_cost_aware_long() -> None:
    decision = decide_baseline(
        symbol="AAA",
        as_of=NOW,
        raw_features=features(),
        statutory_cost_bps=5.0,
        live_spread_bps=4.0,
        paper_slippage_bps_per_side=2.0,
    )
    assert decision.opportunity.status == DecisionStatus.QUALIFIED
    assert decision.opportunity.direction == Direction.LONG
    assert decision.opportunity.expected_net_return_bps > 0


def test_baseline_rejects_when_cost_hurdle_is_not_cleared() -> None:
    decision = decide_baseline(
        symbol="AAA",
        as_of=NOW,
        raw_features=features(rs_vs_benchmark_bps=2.0, rs_vs_sector_bps=2.0),
        statutory_cost_bps=5.0,
        live_spread_bps=4.0,
        paper_slippage_bps_per_side=2.0,
    )
    assert decision.opportunity.status == DecisionStatus.REJECTED
    assert decision.opportunity.reason == "BASELINE_COST_HURDLE_NOT_CLEARED"


def test_baseline_rejects_missing_evidence() -> None:
    decision = decide_baseline(
        symbol="AAA",
        as_of=NOW,
        raw_features={},
        statutory_cost_bps=5.0,
        live_spread_bps=4.0,
        paper_slippage_bps_per_side=2.0,
    )
    assert decision.opportunity.status == DecisionStatus.REJECTED
    assert decision.opportunity.reason.startswith("MISSING_BASELINE_FEATURES:")
