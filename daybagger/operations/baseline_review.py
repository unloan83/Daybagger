from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

INDIA = ZoneInfo("Asia/Kolkata")

SPREAD_COST_MARKERS = (
    "SPREAD_",
    "COST_",
    "EDGE_BELOW_LIVE_COST",
    "FRESH_BASELINE_RECHECK_REJECTED",
)
ALLOCATION_MARKERS = (
    "DAILY_",
    "RISK_",
    "NO_AVAILABLE_CAPITAL",
    "ZERO_EXECUTABLE_QUANTITY",
    "ZERO_DEPLOYABLE_CAPITAL",
    "AGGREGATE_",
    "OPPORTUNITY_",
)
REGIME_MARKERS = (
    "REGIME_",
    "RELATIVE_VOLUME_TOO_LOW",
    "RESIDUAL_STRENGTH_NOT_ALIGNED_WITH_REGIME",
)


@dataclass(frozen=True, slots=True)
class PromotionBar:
    min_sessions: int = 3
    min_total_fills: int = 3
    min_avg_fills_per_session: float = 1.0
    min_avg_predicted_edge_bps: float = 0.0
    min_realized_net_pnl_inr: Decimal = Decimal("0")
    max_loss_days: int = 1


@dataclass(frozen=True, slots=True)
class PromotionAssessment:
    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyBaselineReview:
    session_date: date
    scanned: int
    executable: int
    aligned: int
    qualified: int
    rejected_regime: int
    rejected_spread_cost: int
    rejected_allocation: int
    rejected_execution: int
    rejected_evidence: int
    rejected_other: int
    executed: int
    closed_trades: int
    avg_predicted_edge_bps: float | None
    realized_gross_pnl_inr: Decimal
    realized_net_pnl_inr: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "session_date": self.session_date.isoformat(),
            "scanned": self.scanned,
            "executable": self.executable,
            "aligned": self.aligned,
            "qualified": self.qualified,
            "rejected_regime": self.rejected_regime,
            "rejected_spread_cost": self.rejected_spread_cost,
            "rejected_allocation": self.rejected_allocation,
            "rejected_execution": self.rejected_execution,
            "rejected_evidence": self.rejected_evidence,
            "rejected_other": self.rejected_other,
            "executed": self.executed,
            "closed_trades": self.closed_trades,
            "avg_predicted_edge_bps": self.avg_predicted_edge_bps,
            "realized_gross_pnl_inr": str(self.realized_gross_pnl_inr),
            "realized_net_pnl_inr": str(self.realized_net_pnl_inr),
        }


@dataclass(frozen=True, slots=True)
class BaselineRuntimeReview:
    days: tuple[DailyBaselineReview, ...]
    reject_reasons: Mapping[str, int]
    promotion: PromotionAssessment

    def to_dict(self) -> dict[str, object]:
        return {
            "days": [day.to_dict() for day in self.days],
            "reject_reasons": dict(self.reject_reasons),
            "promotion": {
                "passed": self.promotion.passed,
                "failures": list(self.promotion.failures),
            },
        }


def classify_reject_reason(reason: str) -> str:
    text = reason.strip().upper()
    if not text:
        return "other"
    if ":INSUFFICIENT_EVIDENCE:" in text or text.startswith("INSUFFICIENT_"):
        return "evidence"
    if "EXECUTION_FAIL_CLOSED" in text:
        return "execution"
    if any(marker in text for marker in SPREAD_COST_MARKERS):
        return "spread_cost"
    if any(marker in text for marker in ALLOCATION_MARKERS):
        return "allocation"
    if any(marker in text for marker in REGIME_MARKERS):
        return "regime"
    return "other"


def summarize_reject_buckets(reasons: Iterable[str]) -> dict[str, int]:
    counter = Counter(classify_reject_reason(reason) for reason in reasons)
    return {
        "regime": counter.get("regime", 0),
        "spread_cost": counter.get("spread_cost", 0),
        "allocation": counter.get("allocation", 0),
        "execution": counter.get("execution", 0),
        "evidence": counter.get("evidence", 0),
        "other": counter.get("other", 0),
    }


def review_baseline_runtime(
    *,
    trace_db: Path,
    ledger_db: Path,
    summary_log: Path | None = None,
    timezone_name: str = "Asia/Kolkata",
    promotion_bar: PromotionBar | None = None,
) -> BaselineRuntimeReview:
    timezone = ZoneInfo(timezone_name)
    day_state: dict[date, dict[str, object]] = defaultdict(_empty_day_state)
    reject_counter: Counter[str] = Counter()

    for entry in _load_summary_rows(summary_log, timezone):
        state = day_state[entry["session_date"]]
        state["scanned"] = int(state["scanned"]) + int(entry["scanned"])
        state["executable"] = int(state["executable"]) + int(entry["executable"])
        state["aligned"] = int(state["aligned"]) + int(entry["aligned"])

    for row in _load_trace_rows(trace_db, timezone):
        state = day_state[row["session_date"]]
        if row["qualified"]:
            state["qualified"] = int(state["qualified"]) + 1
        if row["executed"]:
            state["trace_executed"] = int(state["trace_executed"]) + 1
            predicted_edges = state["predicted_edges"]
            assert isinstance(predicted_edges, list)
            predicted_edges.append(row["expected_net_return_bps"])
        elif row["reason"]:
            bucket = classify_reject_reason(row["reason"])
            state[bucket] = int(state[bucket]) + 1
            reject_counter[row["reason"]] += 1

    for row in _load_ledger_rows(ledger_db, timezone):
        fills = day_state[row["opened_date"]]
        fills["executed"] = int(fills["executed"]) + int(row["executed"])
        closed = day_state[row["closed_date"]]
        closed["closed_trades"] = int(closed["closed_trades"]) + int(row["closed_trades"])
        closed["realized_gross_pnl_inr"] = Decimal(closed["realized_gross_pnl_inr"]) + row["gross_pnl_inr"]
        closed["realized_net_pnl_inr"] = Decimal(closed["realized_net_pnl_inr"]) + row["net_pnl_inr"]

    days = tuple(
        _materialize_day(session_date, state)
        for session_date, state in sorted(day_state.items())
    )
    promotion = assess_promotion(days, promotion_bar or PromotionBar())
    return BaselineRuntimeReview(days=days, reject_reasons=dict(reject_counter.most_common()), promotion=promotion)


def assess_promotion(days: Sequence[DailyBaselineReview], bar: PromotionBar) -> PromotionAssessment:
    active = [day for day in days if day.scanned or day.qualified or day.executed or day.closed_trades]
    failures: list[str] = []
    if len(active) < bar.min_sessions:
        failures.append(f"sessions<{bar.min_sessions}")
    total_fills = sum(day.executed for day in active)
    if total_fills < bar.min_total_fills:
        failures.append(f"fills<{bar.min_total_fills}")
    avg_fills = (total_fills / len(active)) if active else 0.0
    if avg_fills < bar.min_avg_fills_per_session:
        failures.append(f"avg_fills<{bar.min_avg_fills_per_session:.2f}")
    predicted_edges = [day.avg_predicted_edge_bps for day in active if day.avg_predicted_edge_bps is not None]
    avg_predicted = mean(predicted_edges) if predicted_edges else 0.0
    if avg_predicted < bar.min_avg_predicted_edge_bps:
        failures.append(f"avg_predicted_edge<{bar.min_avg_predicted_edge_bps:.2f}")
    total_net = sum((day.realized_net_pnl_inr for day in active), start=Decimal("0"))
    if total_net < bar.min_realized_net_pnl_inr:
        failures.append(f"net_pnl<{bar.min_realized_net_pnl_inr}")
    loss_days = sum(1 for day in active if day.realized_net_pnl_inr < 0)
    if loss_days > bar.max_loss_days:
        failures.append(f"loss_days>{bar.max_loss_days}")
    return PromotionAssessment(passed=not failures, failures=tuple(failures))


def _load_summary_rows(summary_log: Path | None, timezone: ZoneInfo) -> list[dict[str, object]]:
    if summary_log is None or not summary_log.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in summary_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        as_of = datetime.fromisoformat(str(payload["as_of"])).astimezone(timezone)
        rows.append(
            {
                "session_date": as_of.date(),
                "scanned": int(payload.get("observed_universe", 0)),
                "executable": int(payload.get("executable_universe", 0)),
                "aligned": int(payload.get("aligned_symbols", 0)),
            }
        )
    return rows


def _load_trace_rows(trace_db: Path, timezone: ZoneInfo) -> list[dict[str, object]]:
    if not trace_db.exists():
        return []
    with sqlite3.connect(trace_db) as conn:
        rows = conn.execute(
            """
            SELECT as_of, reason, status, allocation_approved, expected_net_return_bps
            FROM decision_traces
            ORDER BY as_of, id
            """
        ).fetchall()
    result: list[dict[str, object]] = []
    for as_of_raw, reason, status, allocation_approved, expected_net_return_bps in rows:
        as_of = datetime.fromisoformat(str(as_of_raw)).astimezone(timezone)
        result.append(
            {
                "session_date": as_of.date(),
                "reason": str(reason or ""),
                "qualified": str(status) == "QUALIFIED",
                "executed": bool(allocation_approved),
                "expected_net_return_bps": float(expected_net_return_bps),
            }
        )
    return result


def _load_ledger_rows(ledger_db: Path, timezone: ZoneInfo) -> list[dict[str, object]]:
    if not ledger_db.exists():
        return []
    with sqlite3.connect(ledger_db) as conn:
        open_rows = conn.execute(
            "SELECT opened_at, COUNT(*) FROM positions GROUP BY opened_at"
        ).fetchall()
        trade_rows = conn.execute(
            "SELECT closed_at, gross_pnl_inr, net_pnl_inr FROM trades ORDER BY closed_at"
        ).fetchall()
    result: list[dict[str, object]] = []
    for opened_at_raw, executed in open_rows:
        opened_at = datetime.fromisoformat(str(opened_at_raw)).astimezone(timezone)
        result.append(
            {
                "opened_date": opened_at.date(),
                "closed_date": opened_at.date(),
                "executed": int(executed),
                "closed_trades": 0,
                "gross_pnl_inr": Decimal("0"),
                "net_pnl_inr": Decimal("0"),
            }
        )
    for closed_at_raw, gross_raw, net_raw in trade_rows:
        closed_at = datetime.fromisoformat(str(closed_at_raw)).astimezone(timezone)
        result.append(
            {
                "opened_date": closed_at.date(),
                "closed_date": closed_at.date(),
                "executed": 0,
                "closed_trades": 1,
                "gross_pnl_inr": Decimal(str(gross_raw or "0")),
                "net_pnl_inr": Decimal(str(net_raw or gross_raw or "0")),
            }
        )
    return result


def _empty_day_state() -> dict[str, object]:
    return {
        "scanned": 0,
        "executable": 0,
        "aligned": 0,
        "qualified": 0,
        "regime": 0,
        "spread_cost": 0,
        "allocation": 0,
        "execution": 0,
        "evidence": 0,
        "other": 0,
        "trace_executed": 0,
        "executed": 0,
        "closed_trades": 0,
        "predicted_edges": [],
        "realized_gross_pnl_inr": Decimal("0"),
        "realized_net_pnl_inr": Decimal("0"),
    }


def _materialize_day(session_date: date, state: Mapping[str, object]) -> DailyBaselineReview:
    predicted_edges = state["predicted_edges"]
    assert isinstance(predicted_edges, list)
    avg_predicted = mean(predicted_edges) if predicted_edges else None
    return DailyBaselineReview(
        session_date=session_date,
        scanned=int(state["scanned"]),
        executable=int(state["executable"]),
        aligned=int(state["aligned"]),
        qualified=int(state["qualified"]),
        rejected_regime=int(state["regime"]),
        rejected_spread_cost=int(state["spread_cost"]),
        rejected_allocation=int(state["allocation"]),
        rejected_execution=int(state["execution"]),
        rejected_evidence=int(state["evidence"]),
        rejected_other=int(state["other"]),
        executed=max(int(state["executed"]), int(state["trace_executed"])),
        closed_trades=int(state["closed_trades"]),
        avg_predicted_edge_bps=avg_predicted,
        realized_gross_pnl_inr=Decimal(state["realized_gross_pnl_inr"]),
        realized_net_pnl_inr=Decimal(state["realized_net_pnl_inr"]),
    )
