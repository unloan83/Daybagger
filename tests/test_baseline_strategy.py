from datetime import datetime, timezone

import pytest

from daybagger.decision.baseline import RelativeStrengthBaselineDecider
from daybagger.domain import DecisionStatus, Direction
from daybagger.operations.readiness import run_readiness


NOW = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)


def _raw(**overrides):
    base = {
        "rs_vs_benchmark_bps": 80.0,
        "rs_vs_sector_bps": 60.0,
        "relative_volume": 1.6,
        "stock_session_range_bps": 120.0,
        "stock_return_5m_bps": 25.0,
        "stock_return_15m_bps": 40.0,
        "market_return_15m_bps": 35.0,
        "market_trend_efficiency": 0.6,
        "bank_nifty_return_15m_bps": 20.0,
        "breadth_advance_ratio": 0.68,
        "cross_section_dispersion_bps": 90.0,
    }
    base.update(overrides)
    return base


def test_baseline_qualifies_strong_long_relative_strength() -> None:
    decision = RelativeStrengthBaselineDecider(paper_slippage_bps_per_side=2.0).decide(
        symbol="AAA",
        as_of=NOW,
        raw_features=_raw(),
        statutory_cost_bps=18.0,
        live_spread_bps=3.0,
    )
    assert decision.regime.allowed is True
    assert decision.opportunity.status == DecisionStatus.QUALIFIED
    assert decision.opportunity.direction == Direction.LONG
    assert decision.opportunity.expected_net_return_bps > 0
    assert decision.opinions[0].model_id == "baseline_relative_strength"


def test_baseline_qualifies_strong_short_relative_strength() -> None:
    decision = RelativeStrengthBaselineDecider(paper_slippage_bps_per_side=2.0).decide(
        symbol="BBB",
        as_of=NOW,
        raw_features=_raw(
            rs_vs_benchmark_bps=-90.0,
            rs_vs_sector_bps=-70.0,
            stock_return_5m_bps=-20.0,
            stock_return_15m_bps=-35.0,
            market_return_15m_bps=-30.0,
            bank_nifty_return_15m_bps=-15.0,
            breadth_advance_ratio=0.32,
        ),
        statutory_cost_bps=18.0,
        live_spread_bps=3.0,
    )
    assert decision.regime.allowed is True
    assert decision.opportunity.status == DecisionStatus.QUALIFIED
    assert decision.opportunity.direction == Direction.SHORT
    assert decision.opportunity.expected_net_return_bps > 0


def test_baseline_rejects_unclear_regime_and_wide_spread() -> None:
    decider = RelativeStrengthBaselineDecider(paper_slippage_bps_per_side=2.0)
    unclear = decider.decide(
        symbol="CCC",
        as_of=NOW,
        raw_features=_raw(
            market_return_15m_bps=4.0,
            breadth_advance_ratio=0.5,
            bank_nifty_return_15m_bps=1.0,
        ),
        statutory_cost_bps=18.0,
        live_spread_bps=3.0,
    )
    assert unclear.opportunity.status == DecisionStatus.REJECTED
    assert unclear.opportunity.reason == "REGIME_LOW_BENCHMARK_IMPULSE"

    wide = decider.decide(
        symbol="DDD",
        as_of=NOW,
        raw_features=_raw(stock_session_range_bps=40.0),
        statutory_cost_bps=18.0,
        live_spread_bps=20.0,
    )
    assert wide.opportunity.status == DecisionStatus.REJECTED
    assert wide.opportunity.reason == "SPREAD_TOO_WIDE_FOR_BASELINE_EDGE"


def test_readiness_passes_without_meta_model_when_token_exists(tmp_path) -> None:
    (tmp_path / "goldenrules.txt").write_text("rules", encoding="utf-8")
    report = run_readiness(
        repo_root=tmp_path,
        access_token_present=True,
        meta_spec=None,
    )
    assert report.ready is True
    assert "BASELINE_RELATIVE_STRENGTH_RUNTIME_READY" in report.checks
    assert "UPSTOX_TOKEN_PRESENT" in report.checks
