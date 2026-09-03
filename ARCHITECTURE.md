# Daybagger Canonical Architecture

There is exactly **one production decision path**.

1. **Official observations** — NSE/Upstox data only; missing/invalid evidence fails closed.
2. **Broad intelligence** — market, sector, stock, cross-section, institutional/flow, volatility and executable microstructure; additional sources are evidence-gated.
3. **Specialist evidence** — validated probabilistic base models, never independent production engines.
4. **Meta intelligence** — nonlinear interaction model combines specialist + context evidence and predicts LONG/SHORT gross opportunity.
5. **Net-edge gate** — subtract conservative statutory cost, real live spread and declared two-sided execution slippage.
6. **Cross-sectional ranking** — compare all qualified opportunities simultaneously.
7. **Portfolio risk** — sequential cash/open-risk reservation, daily-loss halt, drawdown-aware allocation, integer quantity sizing and actual-cost recheck.
8. **Paper execution** — fresh quote required; actual broker fill is authoritative; live orders are absent.
9. **Ledger & exits** — authoritative fills, costs, stop/horizon/EOD exits and exact realised P&L.
10. **Learning** — accepted and rejected opportunities are labelled from genuine future candles; influence requires sufficient recent evidence and conservative uncertainty adjustment.

## Model research protocol

- Fixed candidate horizons are declared before results.
- Chronological expanding OOF development.
- Final time holdout plus unseen-symbol cohort.
- Historical bid/ask is never fabricated.
- Historical validation includes statutory/brokerage cost plus declared two-sided paper-slippage allowance.
- Live decisions additionally use actual spread.
- Holdout tuning, dummy promotion and post-result gate relaxation are forbidden.

## Runtime/resource design

- Broad quote scan covers the official NSE MIS universe in Upstox-sized batches.
- Expensive minute-candle analysis is capped to a focused liquid subset.
- Training is offline/research-side; the production forest is exported to a standard-library JSON-style spec so OCI runtime remains lightweight.
- Official market timings/status gate sessions; no hand-maintained holiday assumptions.
- Bounded retry/backoff applies only to transient transport/429/5xx failures; authentication/data-integrity errors fail closed.

## Hard invariants

- `goldenrules.txt` must exist and be non-empty.
- Paper mode only.
- Meta model must be genuinely validated before runtime readiness can pass.
- Candidate ≠ trade; prediction ≠ order; order ≠ fill.
- No synthetic candles, quotes, spread, outcomes, validation evidence or confidence.
- No duplicate decision runtimes.
- Secrets remain local and untracked.
