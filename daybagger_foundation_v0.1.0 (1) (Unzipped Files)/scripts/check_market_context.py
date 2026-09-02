from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.data.upstox import UpstoxDataError, UpstoxMarketData
from daybagger.intelligence.market_context import MarketContextEngine


DEFAULT_CONTEXT = (
    "NSE_INDEX|Nifty 50",
    "NSE_INDEX|Nifty Bank",
    "NSE_INDEX|India VIX",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute raw Daybagger market-context features."
    )
    parser.add_argument(
        "--instrument-key",
        action="append",
        dest="instrument_keys",
        help="Repeat for additional NSE/global context instruments.",
    )
    args = parser.parse_args()

    verify_golden_rules(REPO_ROOT)
    keys = tuple(args.instrument_keys or DEFAULT_CONTEXT)

    try:
        engine = MarketContextEngine(UpstoxMarketData())
        context = engine.build(keys)
    except (UpstoxDataError, Exception) as exc:
        print(f"DAYBAGGER MARKET CONTEXT CHECK: FAIL - {exc}")
        return 1

    print("DAYBAGGER MARKET CONTEXT CHECK: PASS")
    print("classification_or_trade_thresholds=NONE")
    for key, features in context.items():
        print(json.dumps(features.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
