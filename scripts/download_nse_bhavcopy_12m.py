from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.bootstrap import verify_golden_rules

NSE_FO_URL_TEMPLATE = "https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}


def download_and_parse_day(d: date) -> dict | None:
    date_str = d.strftime("%Y%m%d")
    url = NSE_FO_URL_TEMPLATE.format(date_str=date_str)
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200 or len(res.content) < 1000:
            return None

        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            namelist = z.namelist()
            if not namelist:
                return None
            
            with z.open(namelist[0]) as f:
                header_line = f.readline().decode("utf-8", errors="replace").strip().split(",")
                try:
                    symb_idx = header_line.index("TckrSymb")
                    opt_idx = header_line.index("OptnTp")
                    spot_idx = header_line.index("UndrlygPric")
                    oi_idx = header_line.index("OpnIntrst")
                except ValueError:
                    return None

                nifty_calls = 0.0
                nifty_puts = 0.0
                nifty_spot = None

                all_calls = 0.0
                all_puts = 0.0

                for line_bytes in f:
                    line_str = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    parts = line_str.split(",")
                    if len(parts) <= max(symb_idx, opt_idx, spot_idx, oi_idx):
                        continue

                    symb = parts[symb_idx].strip()
                    opt = parts[opt_idx].strip()
                    oi_str = parts[oi_idx].strip()

                    if not oi_str:
                        continue
                    try:
                        oi = float(oi_str)
                    except ValueError:
                        continue

                    if opt == "CE":
                        all_calls += oi
                        if symb == "NIFTY":
                            nifty_calls += oi
                    elif opt == "PE":
                        all_puts += oi
                        if symb == "NIFTY":
                            nifty_puts += oi

                    if symb == "NIFTY" and parts[spot_idx].strip():
                        try:
                            nifty_spot = float(parts[spot_idx].strip())
                        except ValueError:
                            pass

                nifty_pcr = (nifty_puts / nifty_calls) if nifty_calls > 0 else None
                macro_pcr = (all_puts / all_calls) if all_calls > 0 else None

                return {
                    "date": d.isoformat(),
                    "nifty": {
                        "total_calls": nifty_calls,
                        "total_puts": nifty_puts,
                        "pcr": nifty_pcr,
                        "spot_price": nifty_spot,
                    },
                    "macro": {
                        "total_calls": all_calls,
                        "total_puts": all_puts,
                        "pcr": macro_pcr,
                    },
                }
    except Exception as err:
        return None


def main() -> int:
    verify_golden_rules(REPO_ROOT)

    print("=========================================================================")
    print(" 12-MONTH HISTORICAL NSE F&O BHAVCOPY DOWNLOAD & PCR AGGREGATION")
    print("=========================================================================")

    out_path = REPO_ROOT / "data" / "historical_oi_12m.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    records_by_date: dict[str, dict] = {}

    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            for r in existing:
                if isinstance(r, dict) and "date" in r:
                    records_by_date[r["date"]] = r
            print(f"Loaded {len(records_by_date)} existing daily records from cache.")
        except Exception:
            pass

    start_date = date(2025, 9, 1)
    end_date = date(2026, 9, 7)

    dates_to_fetch = []
    curr = start_date
    while curr <= end_date:
        if curr.weekday() < 5:
            if curr.isoformat() not in records_by_date:
                dates_to_fetch.append(curr)
        curr += timedelta(days=1)

    print(f"Fetching {len(dates_to_fetch)} missing weekday sessions using multi-threading...")

    completed = 0
    missing_dates = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        future_to_date = {executor.submit(download_and_parse_day, d): d for d in dates_to_fetch}
        for future in as_completed(future_to_date):
            d = future_to_date[future]
            rec = future.result()
            completed += 1
            if rec:
                records_by_date[rec["date"]] = rec
            else:
                missing_dates.append(d.isoformat())

            if completed % 25 == 0 or completed == len(dates_to_fetch):
                print(f"Progress: {completed}/{len(dates_to_fetch)} fetched ({len(records_by_date)} total valid)")

    sorted_recs = [records_by_date[k] for k in sorted(records_by_date)]
    out_path.write_text(json.dumps(sorted_recs, indent=2), encoding="utf-8")

    print("\n=========================================================================")
    print(f" SUMMARY:")
    print(f" Total Valid Trading Sessions Saved: {len(sorted_recs)}")
    print(f" Total Non-trading Weekdays / Holidays Identified: {len(missing_dates)}")
    print(f" Date Range: {sorted_recs[0]['date']} to {sorted_recs[-1]['date']}")
    print(f" Saved dataset to: {out_path}")
    print("=========================================================================")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
