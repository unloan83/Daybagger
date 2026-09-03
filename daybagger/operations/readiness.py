from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from daybagger.bootstrap import verify_golden_rules
from daybagger.decision.model import ValidatedModelSpec


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    checks: tuple[str, ...]
    failures: tuple[str, ...]


def run_readiness(
    *,
    repo_root: Path,
    specs: Sequence[ValidatedModelSpec],
    access_token_present: bool,
) -> ReadinessReport:
    checks: list[str] = []
    failures: list[str] = []

    try:
        rules = verify_golden_rules(repo_root)
        checks.append(f"GOLDENRULES_OK:{rules.sha256[:12]}")
    except Exception as exc:
        failures.append(f"GOLDENRULES_FAIL:{exc}")

    if access_token_present:
        checks.append("UPSTOX_TOKEN_PRESENT")
    else:
        failures.append("UPSTOX_TOKEN_MISSING")

    if specs:
        for spec in specs:
            try:
                spec.validate()
                if not spec.validation_id.strip():
                    raise ValueError("missing validation_id")
            except Exception as exc:
                failures.append(f"MODEL_INVALID:{spec.model_id}:{exc}")
        if not any(item.startswith("MODEL_INVALID") for item in failures):
            checks.append(f"VALIDATED_MODELS:{len(specs)}")
    else:
        failures.append("NO_APPROVED_VALIDATED_MODELS")

    return ReadinessReport(
        ready=not failures,
        checks=tuple(checks),
        failures=tuple(failures),
    )
