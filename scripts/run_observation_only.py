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
from daybagger.data.universe import NSEEquityUniverse, usable_for_execution
from daybagger.data.upstox import UpstoxDataError, UpstoxMarketData
from daybagger.runtime.local_env import read_env_value


NIFTY_KEY = "NSE_INDEX|Nifty 50"


def load_access_token(repo_root: Path) -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if token:
        return token
    return read_env_value(repo_root / ".env.local", "UPSTOX_ACCESS_TOKEN").strip()


def observe_once(market_data: UpstoxMarketData, settings, now: datetime) -> dict:
    universe = NSEEquityUniverse()
    instruments = universe.load_mis_equities()
    observed = universe.observe(
        market_data=market_data,
        instruments=instruments,
        batch_size=500,
        require_complete=False,
    )
    executable = [item for item in observed if usable_for_execution(item)]
    executable.sort(key=lambda item: item.session_turnover_inr, reverse=True)
    context = market_data.full_quotes([NIFTY_KEY], require_complete=False)
    benchmark = context.get(NIFTY_KEY)
    top = []
    for item in executable[: settings.runtime.deep_scan_symbols]:
        top.append(
            {
                "symbol": item.instrument.trading_symbol,
                "instrument_key": item.instrument.instrument_key,
                "last_price": str(item.quote.last_price),
                "spread_bps": item.spread_bps,
                "session_turnover_inr": float(item.session_turnover_inr),
                "as_of": item.quote.as_of.isoformat(),
            }
        )
    return {
        "observed_at": now.isoformat(),
        "mode": "observation_only",
        "paper_orders": 0,
        "observed_universe": len(observed),
        "executable_universe": len(executable),
        "benchmark_last_price": str(benchmark.last_price) if benchmark else None,
        "top_symbols": top,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Upstox evidence without model decisions or orders.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "logs" / "observation_only.jsonl")
    args = parser.parse_args()

    verify_golden_rules(REPO_ROOT)
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    token = load_access_token(REPO_ROOT)
    if not token:
        print("DAYBAGGER OBSERVATION: UPSTOX_ACCESS_TOKEN missing")
        return 3
    market_data = UpstoxMarketData(access_token=token)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    while True:
        now = datetime.now(ZoneInfo(settings.app.timezone))
        try:
            report = observe_once(market_data, settings, now)
            with args.output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(report, sort_keys=True) + "\n")
            print(
                "DAYBAGGER OBSERVATION "
                f"as_of={report['observed_at']} observed={report['observed_universe']} "
                f"executable={report['executable_universe']} paper_orders=0"
            )
        except UpstoxDataError as exc:
            print(f"DAYBAGGER OBSERVATION: FAIL_CLOSED {exc}")
        if args.once:
            return 0
        sleep(settings.runtime.cycle_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
