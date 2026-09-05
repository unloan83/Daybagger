from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.config import load_settings
from daybagger.operations.baseline_review import PromotionBar, review_baseline_runtime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-db", default=None, help="decision trace sqlite path")
    parser.add_argument("--ledger-db", default=None, help="paper ledger sqlite path")
    parser.add_argument(
        "--summary-log",
        default=str(REPO_ROOT / "logs" / "baseline_runtime_summary.jsonl"),
        help="paper runtime JSONL summary log",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--min-sessions", type=int, default=3)
    parser.add_argument("--min-total-fills", type=int, default=3)
    parser.add_argument("--min-avg-fills", type=float, default=1.0)
    parser.add_argument("--min-avg-predicted-edge-bps", type=float, default=0.0)
    parser.add_argument("--min-realized-net-pnl", type=str, default="0")
    parser.add_argument("--max-loss-days", type=int, default=1)
    args = parser.parse_args()

    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    trace_db = _resolve(args.trace_db or settings.storage.decision_trace_path)
    ledger_db = _resolve(args.ledger_db or settings.storage.paper_ledger_path)
    summary_log = _resolve(args.summary_log)
    report = review_baseline_runtime(
        trace_db=trace_db,
        ledger_db=ledger_db,
        summary_log=summary_log,
        timezone_name=settings.app.timezone,
        promotion_bar=PromotionBar(
            min_sessions=args.min_sessions,
            min_total_fills=args.min_total_fills,
            min_avg_fills_per_session=args.min_avg_fills,
            min_avg_predicted_edge_bps=args.min_avg_predicted_edge_bps,
            min_realized_net_pnl_inr=Decimal(args.min_realized_net_pnl),
            max_loss_days=args.max_loss_days,
        ),
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0

    for day in report.days:
        print(
            "BASELINE DAY "
            f"date={day.session_date.isoformat()} scanned={day.scanned} executable={day.executable} "
            f"aligned={day.aligned} qualified={day.qualified} executed={day.executed} "
            f"closed={day.closed_trades} avg_predicted_edge_bps={_fmt(day.avg_predicted_edge_bps)} "
            f"gross_inr={day.realized_gross_pnl_inr} net_inr={day.realized_net_pnl_inr} "
            f"rejects=regime:{day.rejected_regime},spread_cost:{day.rejected_spread_cost},"
            f"allocation:{day.rejected_allocation},execution:{day.rejected_execution},"
            f"evidence:{day.rejected_evidence},other:{day.rejected_other}"
        )
    print(f"PROMOTION passed={report.promotion.passed} failures={list(report.promotion.failures)}")
    print("TOP_REJECT_REASONS")
    for reason, count in list(report.reject_reasons.items())[:10]:
        print(f"  {count:>4} {reason}")
    return 0


def _resolve(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _fmt(value: float | None) -> str:
    return "na" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
