from __future__ import annotations

import sys
import argparse
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.data.universe import NSEEquityUniverse
from daybagger.data.upstox import UpstoxMarketData
from daybagger.intelligence.upstox_external import (
    UpstoxExternalIntelligence,
    load_sector_cache,
    save_sector_cache,
)
from daybagger.runtime.local_env import read_env_value
from daybagger.validation.default_meta_universe import DEFAULT_META_VALIDATION_SYMBOLS
from daybagger.validation.meta_intelligence import validate_meta_intelligence


FROM_DATE = date(2026, 4, 1)
TO_DATE = date(2026, 9, 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--families",
        default="relative_strength,trend_pullback,volume_participation",
        help="Comma-separated specialist families declared before validation.",
    )
    args = parser.parse_args()
    family_ids = tuple(item.strip() for item in args.families.split(",") if item.strip())
    print("DAYBAGGER LOCKED DIRECT-RETURN META VALIDATION")
    print("method=LOCKED_DIRECT_RETURN_CROSS_SECTION_META_V4")
    print("holdout_tuning=FORBIDDEN auto_fix=FORBIDDEN dummy_model=FORBIDDEN")
    print(f"specialist_families={','.join(family_ids)}")

    token = read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN")
    if not token:
        raise SystemExit("UPSTOX_ACCESS_TOKEN missing from local .env.local")
    market_data = UpstoxMarketData(access_token=token)
    official = NSEEquityUniverse().load_mis_equities()
    by_symbol = {item.trading_symbol.upper(): item for item in official}
    missing = [s for s in DEFAULT_META_VALIDATION_SYMBOLS if s not in by_symbol]
    if len(missing) > 3:
        raise SystemExit(f"too many locked research symbols missing from official MIS universe: {missing}")
    selected = [by_symbol[s] for s in DEFAULT_META_VALIDATION_SYMBOLS if s in by_symbol]
    if len(selected) < 24:
        raise SystemExit("fewer than 24 locked research equities are currently resolvable")

    external = UpstoxExternalIntelligence(market_data)
    cache_path = REPO_ROOT / "data" / "sector_cache.json"
    cache = load_sector_cache(cache_path)
    sectors: dict[str, str] = {}
    changed = False
    for instrument in selected:
        sector = cache.get(instrument.isin)
        if not sector:
            sector = external.company_sector(instrument.isin)
            cache[instrument.isin] = sector
            changed = True
        sectors[instrument.trading_symbol] = sector
    if changed:
        save_sector_cache(cache_path, cache)

    # FII/DII is genuine EOD intelligence. It is lagged strictly by the validator,
    # so session D can only see records from D-1 or earlier.
    institutional = external.institutional_history(
        from_date=FROM_DATE,
        to_date=TO_DATE,
    )
    result = validate_meta_intelligence(
        repo_root=REPO_ROOT,
        access_token=token,
        symbol_to_instrument={i.trading_symbol: i.instrument_key for i in selected},
        sector_by_symbol=sectors,
        institutional_history=institutional,
        from_date=FROM_DATE,
        to_date=TO_DATE,
        family_ids=family_ids,
    )
    print(f"validation_id={result.validation_id}")
    print(f"selected_horizon_minutes={result.selected_horizon_minutes}")
    print(
        "development: "
        f"n={result.development.metrics.observations} "
        f"avg_net_bps={result.development.metrics.avg_net_return_bps:.4f} "
        f"pf={result.development.metrics.profit_factor} "
        f"rank_ic={result.development.rank_ic:.4f} "
        f"brier={result.development.metrics.brier_score:.6f} "
        f"baseline_brier={result.development.baseline_brier:.6f}"
    )
    print(
        "holdout: "
        f"n={result.holdout.metrics.observations} "
        f"avg_net_bps={result.holdout.metrics.avg_net_return_bps:.4f} "
        f"pf={result.holdout.metrics.profit_factor} "
        f"rank_ic={result.holdout.rank_ic:.4f} "
        f"brier={result.holdout.metrics.brier_score:.6f} "
        f"baseline_brier={result.holdout.baseline_brier:.6f} "
        f"session_ci95=[{result.holdout.session_ci95_low_bps:.4f},{result.holdout.session_ci95_high_bps:.4f}]"
    )
    print(f"evidence={result.evidence_path}")
    if result.approved:
        print(f"DAYBAGGER META VALIDATION: APPROVED -> {result.approved_path}")
        return 0
    print(f"DAYBAGGER META VALIDATION: NO_APPROVAL - {result.reason}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
