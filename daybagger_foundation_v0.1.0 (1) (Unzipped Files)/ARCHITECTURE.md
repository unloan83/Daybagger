# Daybagger Canonical Architecture

There is exactly one production decision path:

1. **Data** — real observations only; never fabricate missing values.
2. **Intelligence** — market, sector, stock, flow, news, volatility, global and microstructure evidence.
3. **Specialist Models** — versioned probabilistic opinions with evidence references.
4. **Meta Engine** — combines validated opinions and ranks expected net opportunity.
5. **Risk** — sizes/rejects using capital state, drawdown, liquidity, volatility and opportunity quality.
6. **Execution** — paper-only until explicit promotion criteria are met; requires executable quote evidence.
7. **Outcome** — reconciles what actually happened.
8. **Learning** — measures accepted/rejected opportunities; proposes challengers; never fabricates statistics.

## Hard invariants

- `goldenrules.txt` must exist and be non-empty; startup logs its SHA-256.
- trading mode must be `paper`.
- missing evidence means reject / insufficient evidence.
- candidate != trade; model opinion != order; order != fill.
- backtest/replay/live must eventually use the same model implementation.
- control-plane persistence stays lightweight.
