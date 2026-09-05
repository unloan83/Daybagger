from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from daybagger.bootstrap import verify_golden_rules
from daybagger.meta.stack import MetaIntelligenceSpec


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    checks: tuple[str, ...]
    failures: tuple[str, ...]


def run_readiness(
    *,
    repo_root: Path,
    access_token_present: bool,
    meta_spec: MetaIntelligenceSpec | None = None,
) -> ReadinessReport:
    """Production readiness for the canonical baseline paper runtime."""
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

    checks.append("BASELINE_RELATIVE_STRENGTH_RUNTIME_READY")

    if meta_spec is not None:
        try:
            meta_spec.validate()
            checks.append(f"OPTIONAL_VALIDATED_META_MODEL:{meta_spec.validation_id}")
        except Exception as exc:
            checks.append(f"OPTIONAL_META_MODEL_INVALID:{exc}")

    return ReadinessReport(
        ready=not failures,
        checks=tuple(checks),
        failures=tuple(failures),
    )
