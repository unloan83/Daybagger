# Daybagger

Daybagger is a **paper-only Indian-equity intraday trading system** built around one canonical production path:

**official Upstox market data → market/sector/stock cross-sectional intelligence → deterministic relative-strength baseline decision engine → net-edge cost gate → portfolio-aware risk/quantity sizing → paper execution → ledger → future-candle outcome learning**

## Current operating state

- **Real paper runtime**: `scripts/run_paper_runtime.py`
- **Canonical runtime decision model**: Deterministic cross-sectional relative-strength baseline engine (`decide_baseline()`).
- **Evidence review**: `scripts/review_baseline_runtime.py`
- **Historical replay**: `scripts/replay_baseline.py`
- **Research-only meta validation**: `scripts/validate_meta_intelligence.py` (optional research path for training direct-return meta model artifacts).
- **Meta artifact status**: `config/validated_meta_model.json` is an optional research output and is **not required** by the canonical paper runtime.
- **Live broker execution**: **Disabled** (paper mode only).
- **Missing/invalid evidence**: **Fail closed / NO TRADE**.
- **Decision tracing**: Every evaluated decision (both qualified and rejected) is persisted in `data/decision_traces.sqlite3`; cycle summaries are appended to `logs/baseline_runtime_summary.jsonl`.
- **Universe scan**: Broad official NSE MIS equity quote scan with resource-bounded deep minute-candle analysis.
- **Risk bounds**: ₹30,000 default capital, ₹500 max risk per trade, ₹1,000 hard daily loss limit, max 3 open positions.
- **Sizing & costs**: Actual integer share quantity sizing and statutory/slippage cost recheck before paper execution.
- **Outcome learning**: Future-candle outcome labelling across both executed and rejected opportunities stored in `data/learning.sqlite3`.

## One authoritative decision engine

The production decision authority for paper trading is `daybagger.decision.baseline.decide_baseline()` (or `daybagger.meta.stack.decide_meta()` when a validated meta spec is explicitly loaded) as orchestrated by `DaybaggerPaperRuntime`. Legacy or competing decision engines are intentionally absent so replay, paper trading, and research cannot drift into conflicting decision paths.

## Economics & cost modeling

Historical validation and paper trading never invent unavailable bid/ask quotes. Decisions account for official Indian intraday statutory/brokerage charges plus a declared two-sided paper-slippage allowance. Live paper decisions additionally charge the **actual observed bid/ask spread**, re-checking costs at the exact integer quantity before paper execution.

## Broad intelligence rule

New data sources may be collected immediately, but they influence trading decisions only after timestamp-safe historical/out-of-sample validation. Short-history sources are collected for forward learning until they earn statistical weight.

## Local secrets

`.env.local` and `.env.worker` are local-only and ignored by Git. Never commit credentials or API tokens.

## Verification & Execution

```bash
python scripts/check_foundation.py
python scripts/check_runtime.py
python scripts/check_validation.py

# During NSE market hours (09:15-15:30 IST), with UPSTOX_ACCESS_TOKEN set:
python scripts/run_paper_runtime.py --once
python scripts/review_baseline_runtime.py --json

# Research replay uses genuine historical candles and a declared spread scenario:
python scripts/replay_baseline.py --from-date 2026-08-01 --to-date 2026-09-02 --spread-bps 4
```

`goldenrules.txt` remains the permanent design authority.
