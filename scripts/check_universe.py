from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.data.universe import NSEEquityUniverse


def main() -> int:
    verify_golden_rules(REPO_ROOT)

    try:
        instruments = NSEEquityUniverse().load_mis_equities()
    except Exception as exc:
        print(f"DAYBAGGER UNIVERSE CHECK: FAIL - {exc}")
        return 1

    print("DAYBAGGER UNIVERSE CHECK: PASS")
    print(f"official_nse_mis_equities={len(instruments)}")
    print("source=NSE BOD ∩ NSE MIS via official Upstox instrument files")
    print("strategy_filters_applied=NONE")
    print("sample=" + ",".join(item.trading_symbol for item in instruments[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
