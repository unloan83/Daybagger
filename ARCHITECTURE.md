# Daybagger Canonical Architecture

There is exactly **one production decision path** for paper trading.

1. **Official observations** — NSE/Upstox data only; missing/invalid evidence fails closed (`NO_TRADE`).
2. **Canonical paper baseline** — `DaybaggerPaperRuntime` ranks timestamp-aligned cross-sectional residual strength versus NIFTY and sector, gated by market regime, relative volume, executable spread, statutory cost, and paper slippage (`decide_baseline()`). It does not require a pre-trained meta model artifact.
3. **Research-only meta path** — `scripts/validate_meta_intelligence.py` may produce optional validated artifacts (`ForestRegressorSpec` / `config/validated_meta_model.json`), but it is not a runtime prerequisite.
4. **Broad intelligence** — market, sector, stock, cross-section, institutional/flow, volatility and executable microstructure; additional sources are evidence-gated.
5. **Specialist evidence** — validated probabilistic base models, never independent production engines.
6. **Net-edge gate** — subtract conservative statutory cost, real live spread, and declared two-sided execution slippage.
7. **Cross-sectional ranking** — compare all qualified opportunities simultaneously.
8. **Portfolio risk** — sequential cash/open-risk reservation, daily-loss halt, drawdown-aware allocation, integer quantity sizing, and actual-cost recheck.
9. **Paper execution** — fresh quote required (max age 15s, clock skew tolerance 5.0s); actual broker fill is simulated in paper mode; live broker execution is disabled.
10. **Ledger & exits** — authoritative paper fills, costs, stop/horizon/EOD exits and exact realized P&L recorded in SQLite DB.
11. **Learning** — accepted and rejected opportunities are labeled from genuine future candles; influence requires sufficient recent evidence and conservative uncertainty adjustment.

## Model research protocol

- Fixed candidate horizons are declared before results.
- Chronological expanding OOF development.
- Final time holdout plus unseen-symbol cohort.
- Historical bid/ask is never fabricated.
- Historical validation includes statutory/brokerage cost plus declared two-sided paper-slippage allowance.
- Live decisions additionally use actual observed spread.
- Holdout tuning, dummy promotion and post-result gate relaxation are forbidden.

## Runtime/resource design

- Broad quote scan covers the official NSE MIS universe in Upstox-sized batches.
- Expensive minute-candle analysis is capped to a focused liquid subset.
- Training is offline/research-side; the production forest is exported to a standard-library JSON-style spec so OCI runtime remains lightweight.
- Official market timings/status gate sessions (09:15-15:30 IST); mandatory exit at 15:15 IST; no hand-maintained holiday assumptions.
- Bounded retry/backoff applies only to transient transport/429/5xx failures; authentication/data-integrity errors fail closed.

## Hard invariants

- `goldenrules.txt` must exist and be non-empty.
- Paper mode only.
- Candidate ≠ trade; prediction ≠ order; order ≠ fill.
- No synthetic candles, quotes, spread, outcomes, validation evidence or confidence.
- No duplicate decision runtimes.
- Secrets remain local and untracked.
