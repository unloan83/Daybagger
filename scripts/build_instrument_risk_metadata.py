#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path


SCHEMA = "daybagger-instrument-risk-metadata-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build fail-closed sector/correlation risk metadata"
    )
    parser.add_argument("--instrument-master", type=Path, required=True)
    parser.add_argument("--sector-cache", type=Path, required=True)
    parser.add_argument("--historical-cache", type=Path, required=True)
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--to-date", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-overlap-days", type=int, default=60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_date > args.to_date:
        raise SystemExit("from-date cannot exceed to-date")
    symbols = tuple(
        dict.fromkeys(
            value.strip().upper()
            for value in args.symbols.split(",")
            if value.strip()
        )
    )
    if not symbols:
        raise SystemExit("symbols cannot be empty")

    master = _load_json(args.instrument_master)
    sectors = _load_json(args.sector_cache)
    if not isinstance(master, list) or not isinstance(sectors, dict):
        raise SystemExit("invalid instrument master or sector cache")

    equities = {}
    for row in master:
        if not isinstance(row, dict):
            continue
        if (
            str(row.get("segment") or "").upper() == "NSE_EQ"
            and str(row.get("instrument_type") or "").upper() == "EQ"
        ):
            symbol = str(row.get("trading_symbol") or "").strip().upper()
            if symbol in symbols:
                equities[symbol] = {
                    "instrument_key": str(row.get("instrument_key") or "").strip(),
                    "isin": str(row.get("isin") or "").strip().upper(),
                }

    missing_master = sorted(set(symbols) - set(equities))
    if missing_master:
        raise SystemExit(f"instrument master missing symbols: {missing_master}")

    profiles = {}
    returns_by_symbol = {}
    session_counts = {}
    for symbol in symbols:
        item = equities[symbol]
        sector = str(sectors.get(item["isin"]) or "").strip()
        if not sector:
            raise SystemExit(f"sector cache missing current classification for {symbol}")
        cache_path = _cache_path(
            args.historical_cache,
            item["instrument_key"],
            args.from_date,
            args.to_date,
        )
        closes = _daily_closes(cache_path, args.from_date, args.to_date)
        returns = _daily_log_returns(closes)
        if len(returns) < args.min_overlap_days:
            raise SystemExit(
                f"{symbol}: only {len(returns)} daily returns; "
                f"need {args.min_overlap_days}"
            )
        profiles[symbol] = {"isin": item["isin"], "sector": sector}
        returns_by_symbol[symbol] = returns
        session_counts[symbol] = len(closes)

    correlations = {}
    overlap_counts = {}
    for index, left in enumerate(symbols):
        for right in symbols[index + 1:]:
            overlap = sorted(
                set(returns_by_symbol[left]).intersection(returns_by_symbol[right])
            )
            if len(overlap) < args.min_overlap_days:
                raise SystemExit(
                    f"{left}|{right}: only {len(overlap)} overlapping return days"
                )
            value = _pearson(
                [returns_by_symbol[left][day] for day in overlap],
                [returns_by_symbol[right][day] for day in overlap],
            )
            pair = f"{left}|{right}"
            correlations[pair] = round(value, 8)
            overlap_counts[pair] = len(overlap)

    payload = {
        "schema": SCHEMA,
        "generated_at_utc": datetime.now().astimezone().isoformat(),
        "sector_source": "Upstox company profile cache keyed by official NSE ISIN",
        "universe_source": "Upstox official NSE instrument master",
        "correlation_source": "daily closes derived from point-in-time Upstox minute candles",
        "correlation_method": "Pearson correlation of aligned daily log returns",
        "lookback_from": args.from_date.isoformat(),
        "lookback_to": args.to_date.isoformat(),
        "minimum_overlap_days": args.min_overlap_days,
        "instrument_master_sha256": _sha256(args.instrument_master),
        "sector_cache_sha256": _sha256(args.sector_cache),
        "session_counts": session_counts,
        "overlap_counts": overlap_counts,
        "instruments": profiles,
        "correlations": correlations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(
        json.dumps({
            "output": str(args.output),
            "instruments": len(profiles),
            "correlation_pairs": len(correlations),
            "minimum_sessions": min(session_counts.values()),
            "maximum_sessions": max(session_counts.values()),
        }, sort_keys=True)
    )
    return 0


def _load_json(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def _cache_path(root: Path, key: str, start: date, end: date) -> Path:
    identity = f"{key}|{start}|{end}|1"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    path = root / f"{digest}.json.gz"
    if not path.exists():
        raise SystemExit(f"historical cache missing for one required symbol: {path.name}")
    return path


def _daily_closes(path: Path, start: date, end: date) -> dict[date, float]:
    payload = _load_json(path)
    if payload.get("schema") != "daybagger-historical-candles-v1":
        raise SystemExit(f"invalid candle cache schema: {path.name}")
    closes = {}
    for row in payload.get("candles", []):
        timestamp = datetime.fromisoformat(str(row[0]))
        if start <= timestamp.date() <= end:
            close = float(row[4])
            if close > 0:
                closes[timestamp.date()] = close
    return closes


def _daily_log_returns(closes: dict[date, float]) -> dict[date, float]:
    ordered = sorted(closes.items())
    result = {}
    for index in range(1, len(ordered)):
        day, close = ordered[index]
        previous = ordered[index - 1][1]
        result[day] = math.log(close / previous)
    return result


def _pearson(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    if denominator <= 0:
        raise SystemExit("zero-variance return series cannot produce correlation")
    return numerator / denominator


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
