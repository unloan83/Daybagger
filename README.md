# Daybagger

Daybagger is an Indian-equity meta-intelligence trading system.

This foundation intentionally contains **no trading strategy**. It establishes one canonical path:

**data → intelligence → specialist models → meta-ranking → risk → execution → outcome → learning**

## Non-negotiable rules

- `goldenrules.txt` must exist at the repository root.
- Runtime is **paper-only** at this stage.
- Missing/invalid market data must fail closed.
- No synthetic market data, synthetic model evidence, or silent fallbacks.
- No duplicate engines.
- No paid runtime dependency is introduced here.

## Quick start

```bash
python -m daybagger --repo-root .
python scripts/check_foundation.py
pytest
```

Keep your existing `goldenrules.txt` at the repo root. This bundle does not overwrite it.
