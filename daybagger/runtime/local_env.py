from __future__ import annotations

import re
from pathlib import Path


def read_env_value(path: Path, key: str) -> str:
    """Read exactly one KEY from a dotenv file without sourcing/executing it."""
    if not path.exists():
        return ""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=(.*)$")
    matches: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="strict").splitlines():
        match = pattern.match(raw)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        matches.append(value)
    if len(matches) > 1:
        raise RuntimeError(f"multiple {key} entries found in {path}")
    return matches[0] if matches else ""
