from __future__ import annotations

import argparse
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
from daybagger.meta.stack import load_meta_spec
from daybagger.runtime.local_env import read_env_value
from daybagger.runtime.paper_runtime import DaybaggerPaperRuntime, PaperRuntimeError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one paper cycle and exit")
    args = parser.parse_args()

    verify_golden_rules(REPO_ROOT)
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    spec = load_meta_spec(REPO_ROOT / "config" / "validated_meta_model.json")
    if spec is None:
        print("DAYBAGGER PAPER RUNTIME: NO_APPROVED_META_MODEL - fail closed")
        return 2
    token = read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN")
    if not token:
        print("DAYBAGGER PAPER RUNTIME: UPSTOX_ACCESS_TOKEN missing from local .env.local")
        return 3

    runtime = DaybaggerPaperRuntime(
        repo_root=REPO_ROOT,
        settings=settings,
        market_data=UpstoxMarketData(access_token=token),
        meta_spec=spec,
    )
    while True:
        now = datetime.now(ZoneInfo(settings.app.timezone))
        try:
            result = runtime.run_cycle(now=now)
            print(
                "DAYBAGGER PAPER CYCLE "
                f"as_of={result.as_of.isoformat()} observed={result.observed_universe} "
                f"deep={result.deep_symbols} decisions={result.decisions} "
                f"qualified={result.qualified} fills={result.fills} exits={result.exits} "
                f"no_trade={len(result.no_trade_reasons)}"
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
