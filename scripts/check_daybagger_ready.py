from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daybagger.operations.readiness import run_readiness
from daybagger.specialists.loader import load_validated_model_specs


def main() -> int:
    specs = load_validated_model_specs(
        REPO_ROOT / "config" / "validated_models.json"
    )
    report = run_readiness(
        repo_root=REPO_ROOT,
        specs=specs,
        access_token_present=bool(os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()),
    )
    print("DAYBAGGER READINESS:", "PASS" if report.ready else "FAIL")
    for item in report.checks:
        print("PASS", item)
    for item in report.failures:
        print("FAIL", item)
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
