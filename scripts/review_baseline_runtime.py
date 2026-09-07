from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.runtime.telegram import send_telegram_quietly


def main() -> int:
    parser = argparse.ArgumentParser(description="Review Daybagger baseline paper evidence.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--notify-telegram", action="store_true", help="Send Telegram notification with review summary")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    trace_path = root / "data" / "decision_traces.sqlite3"
    summary_path = root / "logs" / "baseline_runtime_summary.jsonl"
    result = {
        "paper_only": True,
        "trace_rows": 0,
        "trace_status": {},
        "reject_reasons": {},
        "fills": 0,
        "outcomes_recorded": 0,
        "cycle_summaries": 0,
        "latest_cycle": None,
    }
    if trace_path.exists():
        with sqlite3.connect(trace_path) as conn:
            rows = conn.execute(
                "SELECT status, reason, allocation_approved, outcome_recorded FROM decision_traces"
            ).fetchall()
        result["trace_rows"] = len(rows)
        result["trace_status"] = dict(Counter(str(row[0]) for row in rows))
        result["reject_reasons"] = dict(
            Counter(str(row[1]) for row in rows if str(row[0]) != "QUALIFIED")
        )
        result["fills"] = sum(1 for row in rows if int(row[2]) == 1)
        result["outcomes_recorded"] = sum(1 for row in rows if int(row[3]) == 1)
    if summary_path.exists():
        summaries = [
            json.loads(line)
            for line in summary_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        result["cycle_summaries"] = len(summaries)
        result["latest_cycle"] = summaries[-1] if summaries else None

    summary_msg = f"BASELINE PAPER REVIEW: traces={result['trace_rows']} cycles={result['cycle_summaries']} fills={result['fills']}"
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(summary_msg)
        print(f"STATUS {json.dumps(result['trace_status'], sort_keys=True)}")
        print(f"REJECTS {json.dumps(result['reject_reasons'], sort_keys=True)}")
        if result["latest_cycle"]:
            print("LATEST", json.dumps(result["latest_cycle"], sort_keys=True))

    if args.notify_telegram:
        send_telegram_quietly(summary_msg, repo_root=root)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

