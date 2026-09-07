from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.meta.stack import load_meta_spec
from daybagger.operations.readiness import run_readiness
from daybagger.runtime.automation import format_readiness_message, maybe_send_telegram
from daybagger.runtime.local_env import read_env_value
from daybagger.runtime.telegram import TelegramNotifier


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--notify-telegram-on-fail",
        action="store_true",
        help="send a Telegram notification only when readiness fails",
    )
    parser.add_argument(
        "--notify-telegram",
        action="store_true",
        help="send a Telegram notification for every readiness run",
    )
    args = parser.parse_args()

    meta_spec = load_meta_spec(REPO_ROOT / "config" / "validated_meta_model.json")
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        token = read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN") or ""
    report = run_readiness(
        repo_root=REPO_ROOT,
        meta_spec=meta_spec,
        access_token_present=bool(token.strip()),
    )
    print("DAYBAGGER READINESS:", "PASS" if report.ready else "FAIL")
    for item in report.checks:
        print("PASS", item)
    for item in report.failures:
        print("FAIL", item)
    if args.notify_telegram or (args.notify_telegram_on_fail and not report.ready):
        maybe_send_telegram(
            format_readiness_message(report),
            notifier=TelegramNotifier(),
        )
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
