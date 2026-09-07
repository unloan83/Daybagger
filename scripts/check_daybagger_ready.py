from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.operations.readiness import run_readiness
from daybagger.runtime.local_env import read_env_value
from daybagger.runtime.telegram import send_telegram_quietly


def main() -> int:
    parser = argparse.ArgumentParser(description="Daybagger Readiness Check")
    parser.add_argument("--notify-telegram-on-fail", action="store_true", help="Send Telegram notification on failure")
    parser.add_argument("--notify-telegram", action="store_true", help="Send Telegram notification")
    args = parser.parse_args()

    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        token = read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN") or ""
    report = run_readiness(
        repo_root=REPO_ROOT,
        access_token_present=bool(token.strip()),
    )
    status_str = "PASS" if report.ready else "FAIL"
    print("DAYBAGGER READINESS:", status_str)
    for item in report.checks:
        print("PASS", item)
    for item in report.failures:
        print("FAIL", item)

    if (not report.ready and args.notify_telegram_on_fail) or args.notify_telegram:
        msg = f"Daybagger Readiness Check: {status_str}\nChecks: {len(report.checks)} passed, {len(report.failures)} failed."
        send_telegram_quietly(msg, repo_root=REPO_ROOT)

    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

