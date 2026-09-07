from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence


def append_runtime_summary(
    path: Path,
    *,
    as_of: datetime,
    observed: int,
    executable: int,
    aligned_deep: int,
    decisions: int,
    qualified: int,
    fills: int,
    exits: int,
    reject_reasons: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buckets = Counter(reason.split(":", 1)[-1] for reason in reject_reasons)
    payload: Mapping[str, object] = {
        "as_of": as_of.isoformat(),
        "observed": observed,
        "scanned": observed,
        "executable": executable,
        "aligned_deep": aligned_deep,
        "decisions": decisions,
        "qualified": qualified,
        "fills": fills,
        "exits": exits,
        "reject_count": len(reject_reasons),
        "reject_buckets": dict(sorted(buckets.items())),
        "paper_only": True,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")