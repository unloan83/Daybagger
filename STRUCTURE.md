# Repository Structure

- **Live runtime:** `daybagger/engine/signal_listener.py`, started only by `daybagger.service`; it consumes candidates from `dualengine.service`.
- **Live libraries:** `daybagger/` contains risk, paper execution, contracts, data, decision, and validation code.
- **Operations:** `scripts/check_*.py`, `scripts/build_instrument_risk_metadata.py`, and `scripts/collect_historical_oi.py`; the reconciled CPR/OI validation entry point is `scripts/run_reconciled_cpr_oi_backtest.py`.
- **Tests:** `tests/`; canonical verification is the CI command sequence in `.github/workflows/ci.yml`.
- **Configuration and deployment:** `config/` and `deploy/`; the latter contains only current supporting research/billing assets, not production trading services.
- **Generated state:** `data/`, `logs/`, and `research/evidence/`; runtime outputs are ignored by Git.
- **Archived:** `archive/scripts/` holds superseded experiments/runtime entry points and `archive/systemd/` holds removed service definitions.
