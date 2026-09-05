# Daybagger

Daybagger is a **paper-only Indian-equity intraday trading system** built around one canonical production path:

**official market data → market/sector/stock intelligence → deterministic relative-strength baseline ranking → portfolio-aware risk/quantity sizing → paper execution → ledger → outcome learning**

## Current operating state

- Real paper runtime: `scripts/run_paper_runtime.py`
- Locked meta validation: `scripts/validate_meta_intelligence.py`
- Canonical paper strategy: built-in cross-sectional relative-strength baseline
- Runtime model artifact: `config/validated_meta_model.json` remains **research-only until genuine OOS approval**
- Live broker execution: **disabled**
- Missing/invalid evidence: **fail closed / no trade**
- Broad official NSE MIS quote scan with resource-bounded deep candle analysis
- ₹30,000 default capital, ₹500 max risk/trade, ₹1,000 hard daily loss limit
- Actual integer quantity sizing and actual-cost recheck before paper execution
- Learning records executed baseline outcomes; meta promotion remains evidence-gated
- Runtime stage summaries append to `logs/baseline_runtime_summary.jsonl`
- Runtime evidence review: `scripts/review_baseline_runtime.py`
- Historical baseline replay: `scripts/replay_baseline_recent.py --from-date YYYY-MM-DD --to-date YYYY-MM-DD`

## One authoritative paper decision engine

The canonical paper decision authority is the staged `DaybaggerPaperRuntime`, which currently uses a deterministic cross-sectional relative-strength baseline. Meta models remain a research track until they produce genuine OOS edge.

## Economics

Historical validation never invents unavailable bid/ask. It includes known Indian intraday statutory/brokerage costs plus the declared two-sided paper-slippage allowance. Live paper decisions additionally charge the **actual observed spread**, then re-check costs at the actual integer quantity before execution.

## Broad intelligence rule

New sources may be collected immediately, but they influence trading only after timestamp-safe historical/OOS validation. Short-history sources such as current news are collected for forward learning until they earn weight. This keeps the system broad without manufacturing evidence.

## Local secrets

`.env.local` and `.env.worker` are local-only and ignored by Git. Never commit credentials.

## Verification

```bash
python scripts/check_foundation.py
python scripts/check_runtime.py
python scripts/check_validation.py
pytest
```

## Evidence workflow

1. Run `python scripts/run_paper_runtime.py` in paper mode to generate staged JSONL summaries and decision traces.
2. Review actual daily bottlenecks with `python scripts/review_baseline_runtime.py`.
3. Replay the same baseline on recent sessions with `python scripts/replay_baseline_recent.py --from-date YYYY-MM-DD --to-date YYYY-MM-DD`.

`goldenrules.txt` remains the permanent design authority.
