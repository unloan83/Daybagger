from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.config import load_settings
from daybagger.data.upstox import UpstoxMarketData
from daybagger.operations.baseline_review import summarize_reject_buckets
from daybagger.runtime.local_env import read_env_value
from daybagger.runtime.paper_runtime import DaybaggerPaperRuntime, PaperRuntimeError


def load_access_token(repo_root: Path) -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if token:
        return token
    return read_env_value(repo_root / ".env.local", "UPSTOX_ACCESS_TOKEN").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one paper cycle and exit")
    parser.add_argument(
        "--summary-output",
        default=str(REPO_ROOT / "logs" / "baseline_runtime_summary.jsonl"),
        help="JSONL file for structured runtime stage summaries",
    )
    args = parser.parse_args()

    verify_golden_rules(REPO_ROOT)
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    token = load_access_token(REPO_ROOT)
    if not token:
        print("DAYBAGGER PAPER RUNTIME: UPSTOX_ACCESS_TOKEN missing from environment and local .env.local")
        return 3

    runtime = DaybaggerPaperRuntime(
        repo_root=REPO_ROOT,
        settings=settings,
        market_data=UpstoxMarketData(access_token=token),
    )
    summary_path = Path(args.summary_output).expanduser()
    if not summary_path.is_absolute():
        summary_path = REPO_ROOT / summary_path
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        now = datetime.now(ZoneInfo(settings.app.timezone))
        try:
            result = runtime.run_cycle(now=now)
            summary = result.as_dict()
            summary["reject_buckets"] = summarize_reject_buckets(result.no_trade_reasons)
            with summary_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(summary, sort_keys=True) + "\n")
            print(
                "DAYBAGGER PAPER CYCLE "
                f"as_of={result.as_of.isoformat()} observed={result.observed_universe} "
                f"executable={result.executable_universe} deep={result.deep_symbols} "
                f"aligned={result.aligned_symbols} decisions={result.decisions} "
                f"qualified={result.qualified} fills={result.fills} exits={result.exits} "
                f"no_trade={len(result.no_trade_reasons)} "
                f"reject_buckets={summary['reject_buckets']}"
            )
            for reason in result.no_trade_reasons[:10]:
                print("NO_TRADE", reason)
        except PaperRuntimeError as exc:
            print(f"DAYBAGGER PAPER CYCLE: FAIL_CLOSED {exc}")
        if args.once:
            return 0
        sleep(settings.runtime.cycle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
