from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

import fcntl
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.config import load_settings
from daybagger.data.upstox import UpstoxMarketData
from daybagger.decision.baseline import BASELINE_MODEL_ID
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

    lock_path = REPO_ROOT / "logs" / "paper_runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        print("DAYBAGGER PAPER RUNTIME: ANOTHER_INSTANCE_RUNNING")
        return 0

    verify_golden_rules(REPO_ROOT)
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    token = load_access_token(REPO_ROOT)
    if not token:
        msg = "DAYBAGGER PAPER RUNTIME: UPSTOX_ACCESS_TOKEN missing from environment and local .env.local"
        print(msg)
        if args.notify_telegram:
            send_telegram_quietly(msg, repo_root=REPO_ROOT)
        return 3

    try:
        git_head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        git_head = "unknown"
    source = "manual" if sys.stdin.isatty() else "cron"

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
        cycle_res_str = "SUCCESS"
        try:
            result = runtime.run_cycle(now=now)
            cycle_res_str = f"OBSERVED_{result.observed_universe}_FILLS_{result.fills}"
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
            
            today_str = now.strftime("%Y-%m-%d")
            heartbeat_flag = REPO_ROOT / "logs" / f"daily_heartbeat_{today_str}.flag"
            if args.notify_telegram and not heartbeat_flag.exists():
                hb_msg = (
                    f"💓 Daybagger Daily Heartbeat [{today_str}]: Runtime active. "
                    f"as_of={now.strftime('%H:%M:%S')} observed={result.observed_universe} "
                    f"qualified={result.qualified} fills={result.fills}"
                )
                if send_telegram_quietly(hb_msg, repo_root=REPO_ROOT):
                    try:
                        heartbeat_flag.touch()
                    except Exception:
                        pass

            if args.notify_telegram and (result.fills > 0 or result.exits > 0 or result.qualified > 0):
                send_telegram_quietly(f"📈 {cycle_msg}", repo_root=REPO_ROOT)

        except PaperRuntimeError as exc:
            cycle_res_str = f"FAIL_CLOSED_{exc}"
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
            cycle_res_str = f"UNEXPECTED_ERROR_{exc}"
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

        heartbeat_msg = (
            f"DAYBAGGER HEARTBEAT timestamp={now.isoformat()} source={source} "
            f"git_head={git_head} model_id={BASELINE_MODEL_ID} result={cycle_res_str}"
        )
        print(heartbeat_msg)

        if args.once:
            return 0
        sleep(settings.runtime.cycle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

