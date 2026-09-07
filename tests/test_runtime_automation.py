from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from daybagger.operations.baseline_review import BaselineRuntimeReview, DailyBaselineReview, PromotionAssessment
from daybagger.operations.readiness import ReadinessReport
from daybagger.runtime.automation import (
    fail_closed_cycle_summary,
    format_cycle_message,
    format_readiness_message,
    format_review_message,
)
from daybagger.runtime.paper_runtime import PaperRuntimeError


NOW = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


def test_fail_closed_cycle_summary_is_structured() -> None:
    summary = fail_closed_cycle_summary(as_of=NOW, reason="NON_TRADING_DAY")

    assert summary["as_of"] == NOW.isoformat()
    assert summary["paper_only"] is True
    assert summary["no_trade_reasons"] == ["NON_TRADING_DAY"]
    assert summary["reject_buckets"]["other"] == 1
    assert "NON_TRADING_DAY" in format_cycle_message(summary)


def test_format_readiness_and_review_messages() -> None:
    readiness = ReadinessReport(
        ready=False,
        checks=("BASELINE_RELATIVE_STRENGTH_RUNTIME_READY",),
        failures=("UPSTOX_TOKEN_MISSING",),
    )
    review = BaselineRuntimeReview(
        days=(
            DailyBaselineReview(
                session_date=NOW.date(),
                scanned=10,
                executable=6,
                aligned=6,
                qualified=2,
                rejected_regime=1,
                rejected_spread_cost=1,
                rejected_allocation=0,
                rejected_execution=0,
                rejected_evidence=0,
                rejected_other=0,
                executed=1,
                closed_trades=1,
                avg_predicted_edge_bps=12.5,
                realized_gross_pnl_inr=0,
                realized_net_pnl_inr=0,
            ),
        ),
        reject_reasons={"REGIME_LOW_MARKET_TREND_EFFICIENCY": 1},
        promotion=PromotionAssessment(False, ("fills<3",)),
    )

    readiness_message = format_readiness_message(readiness)
    review_message = format_review_message(review)

    assert "DAYBAGGER READINESS FAIL" in readiness_message
    assert "UPSTOX_TOKEN_MISSING" in readiness_message
    assert "promotion=False" in review_message
    assert "top_rejects" in review_message


def test_check_daybagger_ready_no_meta_required(tmp_path: Path, monkeypatch) -> None:
    from scripts import check_daybagger_ready

    (tmp_path / "goldenrules.txt").write_text("rules", encoding="utf-8")
    monkeypatch.setattr(check_daybagger_ready, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "token")
    monkeypatch.setattr("sys.argv", ["check_daybagger_ready.py"])

    assert check_daybagger_ready.main() == 0


def test_run_paper_runtime_writes_fail_closed_summary(tmp_path: Path, monkeypatch) -> None:
    from scripts import run_paper_runtime

    summary_path = tmp_path / "summary.jsonl"

    class _Settings:
        class app:
            timezone = "UTC"

        class runtime:
            cycle_seconds = 300

    class _Runtime:
        def __init__(self, **kwargs):
            pass

        def run_cycle(self, *, now=None):
            raise PaperRuntimeError("NON_TRADING_DAY")

    monkeypatch.setattr(run_paper_runtime, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_paper_runtime, "verify_golden_rules", lambda _: None)
    monkeypatch.setattr(run_paper_runtime, "load_settings", lambda _: _Settings())
    monkeypatch.setattr(run_paper_runtime, "load_access_token", lambda _: "token")
    monkeypatch.setattr(run_paper_runtime, "UpstoxMarketData", lambda access_token: object())
    monkeypatch.setattr(run_paper_runtime, "DaybaggerPaperRuntime", _Runtime)
    monkeypatch.setattr(
        "sys.argv",
        ["run_paper_runtime.py", "--once", "--summary-output", str(summary_path)],
    )

    assert run_paper_runtime.main() == 0
    payload = json.loads(summary_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["paper_only"] is True
    assert payload["no_trade_reasons"] == ["NON_TRADING_DAY"]
