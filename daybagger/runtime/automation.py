from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Mapping

from daybagger.operations.baseline_review import BaselineRuntimeReview, summarize_reject_buckets
from daybagger.operations.readiness import ReadinessReport
from daybagger.runtime.paper_runtime import RuntimeCycleResult
from daybagger.runtime.telegram import TelegramError, TelegramNotifier


PAPER_ONLY = True


def success_cycle_summary(result: RuntimeCycleResult) -> dict[str, object]:
    summary = result.as_dict()
    summary["paper_only"] = PAPER_ONLY
    summary["reject_buckets"] = summarize_reject_buckets(result.no_trade_reasons)
    return summary


def fail_closed_cycle_summary(*, as_of: datetime, reason: str) -> dict[str, object]:
    reasons = [reason]
    return {
        "as_of": as_of.isoformat(),
        "observed_universe": 0,
        "executable_universe": 0,
        "deep_symbols": 0,
        "aligned_symbols": 0,
        "decisions": 0,
        "qualified": 0,
        "fills": 0,
        "exits": 0,
        "no_trade_count": len(reasons),
        "no_trade_reasons": reasons,
        "paper_only": PAPER_ONLY,
        "reject_buckets": summarize_reject_buckets(reasons),
    }


def append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")


def format_readiness_message(report: ReadinessReport) -> str:
    checks = ", ".join(report.checks) if report.checks else "none"
    failures = ", ".join(report.failures) if report.failures else "none"
    status = "PASS" if report.ready else "FAIL"
    return f"DAYBAGGER READINESS {status}\nchecks: {checks}\nfailures: {failures}"


def format_cycle_message(summary: Mapping[str, object]) -> str:
    buckets = summary.get("reject_buckets") or {}
    reasons = list(summary.get("no_trade_reasons") or [])
    return (
        "DAYBAGGER PAPER CYCLE\n"
        f"as_of={summary.get('as_of')} observed={summary.get('observed_universe', 0)} "
        f"executable={summary.get('executable_universe', 0)} deep={summary.get('deep_symbols', 0)} "
        f"aligned={summary.get('aligned_symbols', 0)} decisions={summary.get('decisions', 0)} "
        f"qualified={summary.get('qualified', 0)} fills={summary.get('fills', 0)} exits={summary.get('exits', 0)}\n"
        f"reject_buckets={buckets} reasons={reasons[:3]}"
    )


def format_review_message(report: BaselineRuntimeReview) -> str:
    if not report.days:
        return (
            "DAYBAGGER REVIEW\n"
            f"promotion={report.promotion.passed} failures={list(report.promotion.failures)}\n"
            "days=0"
        )
    latest = report.days[-1]
    top_rejects = list(report.reject_reasons.items())[:3]
    return (
        "DAYBAGGER REVIEW\n"
        f"date={latest.session_date.isoformat()} scanned={latest.scanned} executable={latest.executable} "
        f"aligned={latest.aligned} qualified={latest.qualified} executed={latest.executed} "
        f"closed={latest.closed_trades} net_inr={latest.realized_net_pnl_inr}\n"
        f"promotion={report.promotion.passed} failures={list(report.promotion.failures)}\n"
        f"top_rejects={top_rejects}"
    )


def maybe_send_telegram(
    message: str,
    *,
    notifier: TelegramNotifier | None,
    suppress_errors: bool = True,
) -> None:
    if notifier is None or not notifier.configured:
        return
    try:
        notifier.send(message)
    except TelegramError:
        if not suppress_errors:
            raise
