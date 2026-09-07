from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules
from daybagger.data.upstox import UpstoxMarketData
from daybagger.runtime.local_env import read_env_value
from daybagger.runtime.telegram import send_telegram_quietly

INDIA = ZoneInfo("Asia/Kolkata")
NIFTY_KEY = "NSE_INDEX|Nifty 50"


def fetch_oi_for_date(market_data: UpstoxMarketData, expiry: str, on_date: date) -> dict | None:
    encoded_key = quote(NIFTY_KEY, safe="")
    url = f"https://api.upstox.com/v2/market/oi?instrument_key={encoded_key}&expiry={expiry}&date={on_date.isoformat()}"
    try:
        res = market_data.request_json(url)
        if res.get("status") == "success" and isinstance(res.get("data"), dict):
            return res["data"]
    except Exception:
        pass
    return None


def append_error_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now(INDIA).isoformat()}] {message}\n")


def main() -> int:
    verify_golden_rules(REPO_ROOT)

    print("=========================================================================")
    print(" DAYBAGGER HISTORICAL OI / PCR EXPERIMENT PRE-REGISTRATION")
    print("=========================================================================")
    print("Framing: Daily End-of-Day (EOD) Market Regime & Sentiment Filter.")
    print("Hypothesis: Daily Put-Call Ratio (PCR) levels and multi-day PCR trend direction")
    print("            act as a macro regime gate for next-session trade directions.")
    print("Accumulation Rule: Requires >=40 accumulated daily trading sessions of OI data.")
    print("Pass Bar: Next-session strategy holdout net return improves by > +5.0 bps, Rank IC lift > +0.05.")
    print("Kill Condition: Holdout net return lift <= 0.0 bps OR Rank IC lift <= 0.0.")
    print("=========================================================================\n")

    error_log_path = REPO_ROOT / "logs" / "oi_collector_error.log"

    try:
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip() or read_env_value(REPO_ROOT / ".env.local", "UPSTOX_ACCESS_TOKEN")
        if not token:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN missing from environment or local .env.local")

        market_data = UpstoxMarketData(access_token=token)

        start_date = date(2026, 8, 24)
        end_date = date(2026, 9, 7)
        active_expiries = ["2026-09-08", "2026-09-15", "2026-09-22", "2026-09-29"]

        out_records = []
        current_date = start_date

        while current_date <= end_date:
            if current_date.weekday() >= 5:
                current_date += timedelta(days=1)
                continue

            day_record = {
                "date": current_date.isoformat(),
                "expiries_data": {},
            }

            found_any = False
            for exp in active_expiries:
                data = fetch_oi_for_date(market_data, exp, current_date)
                if data and "total_puts" in data and "total_calls" in data:
                    total_puts = data.get("total_puts", 0)
                    total_calls = data.get("total_calls", 0)
                    pcr = (total_puts / total_calls) if total_calls > 0 else None
                    day_record["expiries_data"][exp] = {
                        "spot_closing_price": data.get("spot_closing_price"),
                        "total_puts": total_puts,
                        "total_calls": total_calls,
                        "pcr": pcr,
                        "strikes_count": len(data.get("call_put_oi_data_list", [])),
                        "raw": data,
                    }
                    found_any = True

            if found_any:
                out_records.append(day_record)

            current_date += timedelta(days=1)

        out_path = REPO_ROOT / "data" / "historical_oi_nifty.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out_records, indent=2), encoding="utf-8")

        n_sessions = len(out_records)
        latest_record = out_records[-1] if out_records else None
        latest_pcr_str = "N/A"
        if latest_record and latest_record["expiries_data"]:
            near_exp = list(latest_record["expiries_data"].keys())[0]
            pcr_val = latest_record["expiries_data"][near_exp].get("pcr")
            if pcr_val is not None:
                latest_pcr_str = f"{float(pcr_val):.3f}"

        success_msg = f"✅ OI collector OK — session {n_sessions}/40 collected, PCR={latest_pcr_str}"
        print(success_msg)
        send_telegram_quietly(success_msg, repo_root=REPO_ROOT)
        return 0

    except Exception as e:
        err_msg = f"❌ OI collector FAILED: {e}"
        print(err_msg, file=sys.stderr)
        append_error_log(error_log_path, err_msg)
        send_telegram_quietly(err_msg, repo_root=REPO_ROOT)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
