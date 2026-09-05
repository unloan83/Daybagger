# Daybagger Canonical Architecture

There is exactly **one production paper decision path**.

1. **Official observations** — NSE/Upstox data only; missing/invalid evidence fails closed.
2. **Broad intelligence** — market, sector, stock, cross-section, institutional/flow, volatility and executable microstructure; additional sources are evidence-gated.
3. **Baseline signal** — deterministic relative-strength ranking combines stock, sector, market, volume and executable spread evidence.
4. **Net-edge gate** — subtract conservative statutory cost, real live spread and declared two-sided execution slippage.
5. **Cross-sectional ranking** — compare all qualified opportunities simultaneously.
6. **Portfolio risk** — sequential cash/open-risk reservation, daily-loss halt, drawdown-aware allocation, integer quantity sizing and actual-cost recheck.
7. **Paper execution** — fresh quote required; actual broker fill is authoritative; live orders are absent.
8. **Ledger & exits** — authoritative fills, costs, stop/horizon/EOD exits and exact realised P&L.
9. **Learning** — executed baseline outcomes are labelled from genuine future candles; meta influence remains offline until evidence earns promotion.

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
- Training is offline/research-side; experimental specialist/meta models remain separate from the canonical paper runtime until they pass locked OOS validation.
- Official market timings/status gate sessions; no hand-maintained holiday assumptions.
- Bounded retry/backoff applies only to transient transport/429/5xx failures; authentication/data-integrity errors fail closed.

## Hard invariants

- `goldenrules.txt` must exist and be non-empty.
- Paper mode only.
- Baseline paper runtime must remain deterministic and cost-aware; experimental meta models stay optional until validated.
- Candidate ≠ trade; prediction ≠ order; order ≠ fill.
- No synthetic candles, quotes, spread, outcomes, validation evidence or confidence.
- No duplicate decision runtimes.
- Secrets remain local and untracked.
