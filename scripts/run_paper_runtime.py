from __future__ import annotations

import argparse
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
from daybagger.runtime.local_env import read_env_value
from daybagger.runtime.paper_runtime import DaybaggerPaperRuntime, PaperRuntimeError
from daybagger.runtime.summary import append_runtime_summary
from daybagger.runtime.telegram import send_telegram_quietly


def load_access_token(repo_root: Path) -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if token:
        return token
    return read_env_value(repo_root / ".env.local", "UPSTOX_ACCESS_TOKEN").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Daybagger Paper Runtime")
    parser.add_argument("--once", action="store_true", help="run one paper cycle and exit")
    parser.add_argument("--notify-telegram", action="store_true", help="send Telegram notification for cycle activity and errors")
    args = parser.parse_args()

    verify_golden_rules(REPO_ROOT)
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    token = load_access_token(REPO_ROOT)
    if not token:
        msg = "DAYBAGGER PAPER RUNTIME: UPSTOX_ACCESS_TOKEN missing from environment and local .env.local"
        print(msg)
        if args.notify_telegram:
            send_telegram_quietly(msg, repo_root=REPO_ROOT)
        return 3

    runtime = DaybaggerPaperRuntime(
        repo_root=REPO_ROOT,
        settings=settings,
        market_data=UpstoxMarketData(access_token=token),
    )
    
    if args.notify_telegram:
        send_telegram_quietly(
            f"🚀 Daybagger Paper Runtime started. Mode: {'once' if args.once else 'continuous'}",
            repo_root=REPO_ROOT,
        )

    while True:
        now = datetime.now(ZoneInfo(settings.app.timezone))
        try:
            result = runtime.run_cycle(now=now)
            cycle_msg = (
                "DAYBAGGER PAPER CYCLE "
                f"as_of={result.as_of.isoformat()} observed={result.observed_universe} "
                f"deep={result.deep_symbols} decisions={result.decisions} "
                f"qualified={result.qualified} fills={result.fills} exits={result.exits} "
                f"no_trade={len(result.no_trade_reasons)}"
            )
            print(cycle_msg)
            for reason in result.no_trade_reasons[:10]:
                print("NO_TRADE", reason)
            
            if args.notify_telegram and (result.fills > 0 or result.exits > 0 or result.qualified > 0):
                send_telegram_quietly(f"📈 {cycle_msg}", repo_root=REPO_ROOT)

        except PaperRuntimeError as exc:
            err_msg = f"DAYBAGGER PAPER CYCLE: FAIL_CLOSED {exc}"
            print(err_msg)
            append_runtime_summary(
                REPO_ROOT / "logs" / "baseline_runtime_summary.jsonl",
                as_of=now,
                observed=0,
                executable=0,
                aligned_deep=0,
                decisions=0,
                qualified=0,
                fills=0,
                exits=0,
                reject_reasons=(str(exc),),
            )
            if args.notify_telegram:
                send_telegram_quietly(f"⚠️ {err_msg}", repo_root=REPO_ROOT)

        except Exception as exc:
            err_msg = f"DAYBAGGER PAPER CYCLE: UNEXPECTED_ERROR {exc}"
            print(err_msg, file=sys.stderr)
            append_runtime_summary(
                REPO_ROOT / "logs" / "baseline_runtime_summary.jsonl",
                as_of=now,
                observed=0,
                executable=0,
                aligned_deep=0,
                decisions=0,
                qualified=0,
                fills=0,
                exits=0,
                reject_reasons=(f"UNEXPECTED_ERROR: {exc}",),
            )
            if args.notify_telegram:
                send_telegram_quietly(f"🚨 {err_msg}", repo_root=REPO_ROOT)

        if args.once:
            return 0
        sleep(settings.runtime.cycle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

