from __future__ import annotations

import json
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from daybagger.config import load_settings
from daybagger.domain import DecisionStatus, Direction, Opportunity
from daybagger.operations.baseline_review import PromotionBar, classify_reject_reason, review_baseline_runtime
from daybagger.operations.trace_store import DecisionTraceStore
from daybagger.runtime.ledger import PaperLedger
from daybagger.validation.baseline_replay import replay_baseline_samples
from daybagger.validation.meta_intelligence import MetaSample

INDIA = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_classify_reject_reason_buckets() -> None:
    assert classify_reject_reason("ABC:REGIME_LOW_MARKET_TREND_EFFICIENCY") == "regime"
    assert classify_reject_reason("ABC:SPREAD_TOO_WIDE_FOR_BASELINE_EDGE") == "spread_cost"
    assert classify_reject_reason("ABC:AGGREGATE_OPEN_RISK_LIMIT_REACHED") == "allocation"
    assert classify_reject_reason("ABC:EXECUTION_FAIL_CLOSED:RuntimeError") == "execution"
    assert classify_reject_reason("ABC:INSUFFICIENT_EVIDENCE:KeyError") == "evidence"


def test_review_baseline_runtime_summarizes_daily_evidence(tmp_path: Path) -> None:
    trace_db = tmp_path / "decision_traces.sqlite3"
    ledger_db = tmp_path / "paper.sqlite3"
    summary_log = tmp_path / "baseline_runtime_summary.jsonl"
    traces = DecisionTraceStore(trace_db)
    traces.initialize()
    ledger = PaperLedger(ledger_db)
    ledger.initialize()

    as_of = datetime(2026, 9, 1, 10, 15, tzinfo=INDIA)
    qualified = Opportunity.create(
        symbol="AAA",
        direction=Direction.LONG,
        as_of=as_of,
        expected_net_return_bps=18.0,
        confidence=0.7,
        status=DecisionStatus.QUALIFIED,
        reason="BASELINE_RELATIVE_STRENGTH_EDGE_COVERS_LIVE_COST",
        opinion_ids=[],
    )
    rejected = Opportunity.create(
        symbol="BBB",
        direction=Direction.FLAT,
        as_of=as_of,
        expected_net_return_bps=0.0,
        confidence=0.0,
        status=DecisionStatus.REJECTED,
        reason="REGIME_LOW_MARKET_TREND_EFFICIENCY",
        opinion_ids=[],
    )
    spread_rejected = Opportunity.create(
        symbol="CCC",
        direction=Direction.FLAT,
        as_of=as_of,
        expected_net_return_bps=0.0,
        confidence=0.0,
        status=DecisionStatus.REJECTED,
        reason="SPREAD_TOO_WIDE_FOR_BASELINE_EDGE",
        opinion_ids=[],
    )
    traces.record_decision(
        symbol="AAA",
        instrument_key="NSE_EQ|AAA",
        as_of=as_of,
        opportunity=qualified,
        allocation_approved=True,
        estimated_cost_bps=12.0,
        opinions=(),
        reference_price=Decimal("100"),
    )
    traces.record_decision(
        symbol="BBB",
        instrument_key="NSE_EQ|BBB",
        as_of=as_of,
        opportunity=rejected,
        allocation_approved=False,
        estimated_cost_bps=12.0,
        opinions=(),
        reference_price=Decimal("100"),
    )
    traces.record_decision(
        symbol="CCC",
        instrument_key="NSE_EQ|CCC",
        as_of=as_of,
        opportunity=spread_rejected,
        allocation_approved=False,
        estimated_cost_bps=12.0,
        opinions=(),
        reference_price=Decimal("100"),
    )
    position = ledger.open_fill(
        symbol="AAA",
        direction=Direction.LONG,
        quantity=10,
        filled_price=Decimal("100"),
        now=as_of,
        instrument_key="NSE_EQ|AAA",
        validation_id="baseline",
        reserved_capital_inr=Decimal("1000"),
        max_loss_inr=Decimal("40"),
        stop_price=Decimal("98"),
        horizon_minutes=15,
    )
    ledger.close_fill(
        position_id=position.position_id,
        filled_price=Decimal("101"),
        now=as_of.replace(hour=10, minute=30),
        costs_inr=Decimal("3"),
        exit_reason="HORIZON",
    )
    summary_log.write_text(
        json.dumps(
            {
                "as_of": as_of.isoformat(),
                "observed_universe": 42,
                "executable_universe": 18,
                "aligned_symbols": 12,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = review_baseline_runtime(
        trace_db=trace_db,
        ledger_db=ledger_db,
        summary_log=summary_log,
        promotion_bar=PromotionBar(min_sessions=1, min_total_fills=1, min_avg_fills_per_session=1.0),
    )

    assert len(report.days) == 1
    day = report.days[0]
    assert day.scanned == 42
    assert day.executable == 18
    assert day.aligned == 12
    assert day.qualified == 1
    assert day.executed == 1
    assert day.rejected_regime == 1
    assert day.rejected_spread_cost == 1
    assert day.closed_trades == 1
    assert day.avg_predicted_edge_bps == 18.0
    assert day.realized_net_pnl_inr == Decimal("7")
    assert report.promotion.passed is True


def test_replay_baseline_samples_reports_promotable_scenario() -> None:
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    samples = [
        _sample("AAA", date(2026, 8, 31), datetime(2026, 8, 31, 10, 15, tzinfo=INDIA), 48.0),
        _sample("BBB", date(2026, 8, 31), datetime(2026, 8, 31, 10, 15, tzinfo=INDIA), 10.0),
        _sample("AAA", date(2026, 9, 1), datetime(2026, 9, 1, 10, 15, tzinfo=INDIA), 44.0),
        _sample("BBB", date(2026, 9, 1), datetime(2026, 9, 1, 10, 15, tzinfo=INDIA), 8.0),
    ]
    report = replay_baseline_samples(
        samples=samples,
        settings=settings,
        spread_scenarios_bps=[0.0],
        statutory_cost_bps=6.0,
        horizon_minutes=15,
        promotion_bar=PromotionBar(min_sessions=2, min_total_fills=2, min_avg_fills_per_session=1.0),
    )

    scenario = report.scenarios[0]
    assert report.statutory_cost_bps == 6.0
    assert scenario.executed >= 2
    assert scenario.selected_sessions == 2
    assert scenario.mean_predicted_edge_bps is not None
    assert scenario.mean_realized_net_bps is not None
    assert scenario.total_net_pnl_inr > 0
    assert scenario.promotion.passed is True


def _sample(symbol: str, session_date: date, as_of: datetime, gross_bps: float) -> MetaSample:
    return MetaSample(
        session_date=session_date,
        symbol=symbol,
        as_of=as_of,
        raw_features={
            "rs_vs_benchmark_bps": 95.0 if symbol == "AAA" else 25.0,
            "rs_vs_sector_bps": 75.0 if symbol == "AAA" else 15.0,
            "relative_volume": 1.7 if symbol == "AAA" else 1.2,
            "stock_session_range_bps": 120.0,
            "stock_return_5m_bps": 28.0,
            "stock_return_15m_bps": 36.0,
            "market_return_15m_bps": 32.0,
            "market_trend_efficiency": 0.55,
            "bank_nifty_return_15m_bps": 18.0,
            "breadth_advance_ratio": 0.64,
            "cross_section_dispersion_bps": 90.0,
        },
        entry_price=Decimal("100"),
        long_gross_return_bps=gross_bps,
        short_gross_return_bps=-gross_bps,
        long_net_return_bps=gross_bps - 10.0,
        short_net_return_bps=-gross_bps - 10.0,
    )
