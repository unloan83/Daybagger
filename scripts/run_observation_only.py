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
from daybagger.runtime.telegram import send_telegram_quietly

NIFTY_KEY = "NSE_INDEX|Nifty 50"

# Rupee liquidity threshold for order book depth (default: ₹5 Lakhs visible depth value)
DEFAULT_MIN_DEPTH_RUPEE_VALUE = 500_000.0


def load_access_token(repo_root: Path) -> str:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if token:
        return token
    return read_env_value(repo_root / ".env.local", "UPSTOX_ACCESS_TOKEN").strip()


def append_error_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now().isoformat()}] {message}\n")


def compute_depth_imbalance_with_liquidity_filter(
    depth: dict,
    last_price: float,
    min_rupee_value: float = DEFAULT_MIN_DEPTH_RUPEE_VALUE,
) -> tuple[float, float | None, int, int, float, bool]:
    buy_levels = depth.get("buy", []) if isinstance(depth, dict) else []
    sell_levels = depth.get("sell", []) if isinstance(depth, dict) else []

    buy_qty = sum(int(level.get("quantity", 0)) for level in buy_levels if isinstance(level, dict))
    sell_qty = sum(int(level.get("quantity", 0)) for level in sell_levels if isinstance(level, dict))

    total_qty = buy_qty + sell_qty
    depth_rupee_value = float(total_qty) * last_price if last_price > 0 else 0.0
    both_sides_populated = buy_qty > 0 and sell_qty > 0
    liquidity_passed = (depth_rupee_value >= min_rupee_value) and both_sides_populated

    raw_imbalance = (buy_qty - sell_qty) / total_qty if total_qty > 0 else 0.0
    usable_imbalance = round(raw_imbalance, 6) if liquidity_passed else None

    return raw_imbalance, usable_imbalance, buy_qty, sell_qty, round(depth_rupee_value, 2), liquidity_passed


def observe_once(
    market_data: UpstoxMarketData,
    settings,
    now: datetime,
    depth_output_path: Path,
    min_depth_rupee_value: float = DEFAULT_MIN_DEPTH_RUPEE_VALUE,
) -> dict:
    universe = NSEEquityUniverse()
    instruments = universe.load_mis_equities()
    observed = universe.observe(
        market_data=market_data,
        instruments=instruments,
        batch_size=500,
        require_complete=False,
    )
    executable = [item for item in observed if usable_for_execution(item)]

    targets = executable if executable else observed
    targets.sort(key=lambda item: item.session_turnover_inr, reverse=True)

    context = market_data.full_quotes([NIFTY_KEY], require_complete=False)
    benchmark = context.get(NIFTY_KEY)

    top = []
    deep_items = targets[: settings.runtime.deep_scan_symbols]
    deep_keys = [item.instrument.instrument_key for item in deep_items]

    raw_quotes = market_data.full_quotes_raw(deep_keys) if deep_keys else {}

    depth_records_written = 0
    liquid_depth_records = 0

    for item in deep_items:
        key = item.instrument.instrument_key
        raw_info = raw_quotes.get(key) or raw_quotes.get(f"NSE_EQ:{item.instrument.trading_symbol}") or {}
        raw_depth = raw_info.get("depth", {}) if isinstance(raw_info, dict) else {}

        last_p = float(item.quote.last_price)
        raw_imb, usable_imb, buy_qty, sell_qty, rupee_val, passed = compute_depth_imbalance_with_liquidity_filter(
            raw_depth, last_p, min_depth_rupee_value
        )

        if passed:
            liquid_depth_records += 1

        depth_record = {
            "ts": now.isoformat(),
            "symbol": item.instrument.trading_symbol,
            "instrument_key": key,
            "last_price": str(item.quote.last_price),
            "spread_bps": item.spread_bps,
            "total_buy_quantity": raw_info.get("total_buy_quantity", buy_qty),
            "total_sell_quantity": raw_info.get("total_sell_quantity", sell_qty),
            "total_depth_rupee_value": rupee_val,
            "liquidity_filter_passed": passed,
            "min_rupee_threshold": min_depth_rupee_value,
            "raw_depth_imbalance": round(raw_imb, 6),
            "usable_depth_imbalance": usable_imb,
            "depth": raw_depth,
        }

        depth_output_path.parent.mkdir(parents=True, exist_ok=True)
        with depth_output_path.open("a", encoding="utf-8") as dh:
            dh.write(json.dumps(depth_record, sort_keys=True) + "\n")

        depth_records_written += 1

        top.append(
            {
                "symbol": item.instrument.trading_symbol,
                "instrument_key": key,
                "last_price": str(item.quote.last_price),
                "spread_bps": item.spread_bps,
                "depth_rupee_value": rupee_val,
                "liquidity_passed": passed,
                "usable_depth_imbalance": usable_imb,
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
        "depth_records_collected": depth_records_written,
        "liquid_depth_records_passed": liquid_depth_records,
        "top_symbols": top,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Upstox evidence and live order-book depth without model decisions or orders.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "logs" / "observation_only.jsonl")
    parser.add_argument("--depth-output", type=Path, default=REPO_ROOT / "data" / "depth_history.jsonl")
    parser.add_argument("--min-depth-rupees", type=float, default=DEFAULT_MIN_DEPTH_RUPEE_VALUE)
    parser.add_argument("--notify-telegram", action="store_true", help="Send Telegram notification for cycle activity")
    args = parser.parse_args()

    error_log_path = REPO_ROOT / "logs" / "depth_collector_error.log"

    try:
        verify_golden_rules(REPO_ROOT)
        settings = load_settings(REPO_ROOT / "config" / "default.toml")
        token = load_access_token(REPO_ROOT)
        if not token:
            msg = "DAYBAGGER OBSERVATION: UPSTOX_ACCESS_TOKEN missing"
            print(msg)
            append_error_log(error_log_path, msg)
            send_telegram_quietly(f"❌ Depth collector FAILED: {msg}", repo_root=REPO_ROOT)
            return 3
        market_data = UpstoxMarketData(access_token=token)
        args.output.parent.mkdir(parents=True, exist_ok=True)

        while True:
            now = datetime.now(ZoneInfo(settings.app.timezone))
            try:
                report = observe_once(market_data, settings, now, args.depth_output, args.min_depth_rupees)
                with args.output.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(report, sort_keys=True) + "\n")
                
                status_msg = (
                    "DAYBAGGER OBSERVATION "
                    f"as_of={report['observed_at']} observed={report['observed_universe']} "
                    f"executable={report['executable_universe']} depth_collected={report['depth_records_collected']} "
                    f"liquid_passed={report['liquid_depth_records_passed']} paper_orders=0"
                )
                print(status_msg)

                if args.notify_telegram:
                    telegram_msg = (
                        f"✅ Depth collector OK — as_of={report['observed_at'][:16]} "
                        f"collected={report['depth_records_collected']} "
                        f"liquid_passed={report['liquid_depth_records_passed']}"
                    )
                    send_telegram_quietly(telegram_msg, repo_root=REPO_ROOT)

            except Exception as exc:
                err_msg = f"DAYBAGGER OBSERVATION FAIL: {exc}"
                print(f"❌ Depth collector FAILED: {exc}", file=sys.stderr)
                append_error_log(error_log_path, err_msg)
                send_telegram_quietly(f"❌ Depth collector FAILED: {exc}", repo_root=REPO_ROOT)

            if args.once:
                return 0
            sleep(settings.runtime.cycle_seconds)

    except Exception as exc:
        fatal_msg = f"❌ Depth collector FATAL: {exc}"
        print(fatal_msg, file=sys.stderr)
        append_error_log(error_log_path, fatal_msg)
        send_telegram_quietly(fatal_msg, repo_root=REPO_ROOT)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
