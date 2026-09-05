from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.config import load_settings
from daybagger.data.universe import NSEEquityUniverse
from daybagger.data.upstox import UpstoxMarketData
from daybagger.intelligence.upstox_external import UpstoxExternalIntelligence, load_sector_cache, save_sector_cache
from daybagger.integration.costs import IndiaEquityIntradayCostModel
from daybagger.runtime.local_env import read_env_value
from daybagger.validation.baseline_replay import replay_baseline_samples
from daybagger.validation.default_meta_universe import DEFAULT_META_VALIDATION_SYMBOLS
from daybagger.validation.historical import HistoricalCandleClient
from daybagger.validation.meta_intelligence import build_meta_samples, group_sessions

NIFTY_KEY = "NSE_INDEX|Nifty 50"
BANK_NIFTY_KEY = "NSE_INDEX|Nifty Bank"
INDIA_VIX_KEY = "NSE_INDEX|India VIX"


def load_access_token(repo_root: Path) -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if token:
        return token
    return read_env_value(repo_root / ".env.local", "UPSTOX_ACCESS_TOKEN").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", type=_parse_date, required=True)
    parser.add_argument("--to-date", type=_parse_date, required=True)
    parser.add_argument("--horizon-minutes", type=int, default=15)
    parser.add_argument("--spread-scenarios", default="0,4,8")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    verify_golden_rules(REPO_ROOT)
    settings = load_settings(REPO_ROOT / "config" / "default.toml")
    token = load_access_token(REPO_ROOT)
    if not token:
        print("UPSTOX_ACCESS_TOKEN missing from environment and local .env.local")
        return 3

    market_data = UpstoxMarketData(access_token=token)
    historical = HistoricalCandleClient(market_data, cache_dir=REPO_ROOT / "data" / "historical_cache")
    official = NSEEquityUniverse().load_mis_equities()
    by_symbol = {item.trading_symbol.upper(): item for item in official}
    selected = [by_symbol[s] for s in DEFAULT_META_VALIDATION_SYMBOLS if s in by_symbol]
    if len(selected) < 24:
        raise SystemExit("fewer than 24 default replay equities are resolvable from the official MIS universe")

    sectors = _resolve_sectors(market_data, selected)
    symbol_to_instrument = {item.trading_symbol: item.instrument_key for item in selected if item.trading_symbol in sectors}
    stocks = {
        symbol: group_sessions(historical.fetch(key, from_date=args.from_date, to_date=args.to_date))
        for symbol, key in symbol_to_instrument.items()
    }
    stocks = {symbol: sessions for symbol, sessions in stocks.items() if sessions}
    if len(stocks) < 6:
        raise SystemExit("fewer than six replay equities returned historical candles")

    contexts = {
        "market": group_sessions(historical.fetch(NIFTY_KEY, from_date=args.from_date, to_date=args.to_date)),
        "bank": group_sessions(historical.fetch(BANK_NIFTY_KEY, from_date=args.from_date, to_date=args.to_date)),
        "vix": group_sessions(historical.fetch(INDIA_VIX_KEY, from_date=args.from_date, to_date=args.to_date)),
    }
    cost_model = IndiaEquityIntradayCostModel()
    validation_position_notional_inr = Decimal(str(settings.capital.starting_capital_inr)) * Decimal(str(settings.risk.max_position_fraction))
    statutory_cost_bps = cost_model.round_trip_bps_for_notional(validation_position_notional_inr)
    samples = build_meta_samples(
        stock_sessions_by_symbol=stocks,
        market_sessions=contexts["market"],
        bank_sessions=contexts["bank"],
        vix_sessions=contexts["vix"],
        sector_by_symbol={symbol: sectors[symbol] for symbol in stocks},
        institutional_history=None,
        horizon_minutes=args.horizon_minutes,
        round_trip_cost_bps=statutory_cost_bps + 2.0 * settings.execution.paper_slippage_bps,
    )
    report = replay_baseline_samples(
        samples=samples,
        settings=settings,
        spread_scenarios_bps=[float(item.strip()) for item in args.spread_scenarios.split(",") if item.strip()],
        statutory_cost_bps=statutory_cost_bps,
        horizon_minutes=args.horizon_minutes,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0

    for scenario in report.scenarios:
        print(
            "BASELINE REPLAY "
            f"spread_bps={scenario.spread_bps:.2f} sessions={scenario.sessions} selected_sessions={scenario.selected_sessions} "
            f"qualified={scenario.qualified} executed={scenario.executed} predicted_edge_bps={_fmt(scenario.mean_predicted_edge_bps)} "
            f"realized_net_bps={_fmt(scenario.mean_realized_net_bps)} total_net_inr={scenario.total_net_pnl_inr} "
            f"max_drawdown={scenario.max_drawdown_fraction:.4f} promotion={scenario.promotion.passed}"
        )
        print(f"  reject_buckets={dict(scenario.reject_buckets)}")
    return 0


def _resolve_sectors(market_data: UpstoxMarketData, instruments):
    external = UpstoxExternalIntelligence(market_data)
    cache_path = REPO_ROOT / "data" / "sector_cache.json"
    cache = load_sector_cache(cache_path)
    changed = False
    sectors: dict[str, str] = {}
    for instrument in instruments:
        sector = cache.get(instrument.isin)
        if not sector:
            sector = external.company_sector(instrument.isin)
            cache[instrument.isin] = sector
            changed = True
        sectors[instrument.trading_symbol] = sector
    if changed:
        save_sector_cache(cache_path, cache)
    return sectors


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def _fmt(value: float | None) -> str:
    return "na" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
