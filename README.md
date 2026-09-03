# Daybagger

Daybagger is a **paper-only Indian-equity meta-intelligence trading system** built around one canonical production path:

**official market data → market/sector/stock/flow intelligence → validated specialists → nonlinear meta-model → cross-sectional net-edge ranking → portfolio-aware risk/quantity sizing → paper execution → ledger → accepted/rejected outcome learning**

## Current operating state

- Real paper runtime: `scripts/run_paper_runtime.py`
- Locked meta validation: `scripts/validate_meta_intelligence.py`
- Runtime model: `config/validated_meta_model.json` **only after genuine OOS approval**
- Live broker execution: **disabled**
- Missing/invalid evidence: **fail closed / no trade**
- Broad official NSE MIS quote scan with resource-bounded deep candle analysis
- ₹30,000 default capital, ₹500 max risk/trade, ₹1,000 hard daily loss limit
- Actual integer quantity sizing and actual-cost recheck before paper execution
- Learning requires sufficient recent evidence and includes rejected opportunities

## One authoritative decision engine

The production decision authority is `daybagger.meta.stack.decide_meta()` as orchestrated by `DaybaggerPaperRuntime`. Legacy single-specialist execution engines are intentionally absent so replay/live evolution cannot drift into competing decision paths.

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

`goldenrules.txt` remains the permanent design authority.
