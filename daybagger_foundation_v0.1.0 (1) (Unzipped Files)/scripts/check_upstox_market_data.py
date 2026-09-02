from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.data.upstox import UpstoxDataError, UpstoxMarketData


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify genuine Upstox quote + intraday candle semantics."
    )
    parser.add_argument(
        "--instrument-key",
        required=True,
        help="Example: NSE_EQ|INE002A01018",
    )
    args = parser.parse_args()

    verify_golden_rules(REPO_ROOT)

    try:
        client = UpstoxMarketData()
        snapshot = client.full_quotes([args.instrument_key])[args.instrument_key]
        candles = client.intraday_candles(args.instrument_key, interval_minutes=1)
    except UpstoxDataError as exc:
        print(f"DAYBAGGER UPSTOX CHECK: FAIL - {exc}")
        return 1

    print("DAYBAGGER UPSTOX CHECK: PASS")
    print(
        f"symbol={snapshot.symbol} "
        f"last={snapshot.last_price} "
        f"session_volume={snapshot.session_volume} "
        f"best_bid={snapshot.best_bid} "
        f"best_ask={snapshot.best_ask}"
    )
    print(
        f"genuine_1m_candles={len(candles)} "
        f"first={candles[0].timestamp.isoformat()} "
        f"last={candles[-1].timestamp.isoformat()}"
    )
    print("semantic_guard=session quote OHLC/volume NOT stored as minute candles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
