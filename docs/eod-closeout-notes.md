# End-of-day close-out notes

This is the running, sanitized operations record. Do not include IP addresses,
OCI identifiers, account/order/signal identifiers, credentials, tokens, or commit
hashes here.

## 2026-09-13 (IST, Sunday)

Scope: one read-only close-out of both locally configured OCI VMs plus the local
Daybagger, DualEngine, and trading-contracts repositories. This was not an
OCI-control-plane tenancy inventory, so resources not represented by the two
configured VM connections were outside the observable scope. The Multibagger
workspace was excluded and not accessed.

### VM1 — production

- `daybagger`, `dualengine`, and `trading_telemetry` were active and running.
  The legacy `daybagger-paper` and `daybagger-runtime` units were disabled and
  inactive.
- All active production units reported zero systemd restart attempts, and the
  lifecycle-only journal check found no start, stop, failure, or restart event
  during the 2026-09-13 IST window.
- The role-file parser could not confirm the expected production role marker.
  Operational service state was production, but the marker should be checked
  later from VM2-side maintenance tooling.
- Open positions: **0**.
- Today's ledger contained **1,770 rows**, all `REJECTED`; these were rejected
  candidates, not executed trades. No duplicate signal keys existed in the
  ledger, and no same-instrument/same-direction/same-minute excess rows were
  found today.
- DualEngine wrote **1,940 shadow records** today. None had a complete positive
  `prev_high`/`prev_low`/`prev_close` set, so the `prev_ohlc` validation remains
  **open**.
- Because this was Sunday, production engines generating repeated rejected
  candidates while the market was closed is unusual and should be reviewed
  after hours. No live diagnosis or correction was attempted.
- Git was **not clean** in any of the three VM1 repositories. Daybagger had 4
  tracked changes and 6 untracked paths; DualEngine had 5 tracked changes and 1
  untracked path; trading-contracts had 2 tracked changes and 1 untracked path.
  Each checkout was on `master`, while the expected remote branch is `main`.
- Each VM1 HEAD matched its stale local `origin/main` reference but did **not**
  match the current live `origin/main`. This is production drift and must be
  reconciled deliberately during a later maintenance window, not tonight.

### VM2 — research/staging

- Daybagger, DualEngine, paper/runtime, and related production service units
  were masked and inactive. No named Daybagger or DualEngine process was found.
- No `.env`, `.env.*`, or credential-named file was found under the research
  checkout within the inspected depth.
- The role-file parser could not confirm the expected research marker, although
  the systemd masks enforce the research-only boundary. Verify the marker format
  during the next VM2 maintenance session.
- Tracked code was clean and matched current live `origin/main`. Two untracked
  billing-guard files remain as expected research/staging work; nothing was
  promoted to production.

### Local workspace

- All three local repositories' tracked HEADs matched current live
  `origin/main`.
- Daybagger had 10 untracked operations/billing-guard/documentation paths;
  DualEngine had one untracked `data/` path; trading-contracts was clean.
- `.env.local` is Git-ignored, mode `0600`, and all four required VM2 SSH
  connection fields are present. Their values were not printed or recorded.

### Deferred VM2-side work (no action tonight)

1. Diagnose why all Sunday DualEngine records lacked valid previous-session
   OHLC values and why the engines continued producing candidates on a closed
   market day.
2. Reconcile VM1's three dirty `master` worktrees against current remote `main`
   with a reviewed backup/diff and an explicit maintenance plan.
3. Standardize the VM role marker key so automated audits can confirm it without
   ambiguity.
4. Review the 1,770 rejection reasons in aggregate on VM2 and confirm that the
   closed-market guard behaves as intended.

Close-out verdict: **not a clean-session milestone**. Service continuity and
zero open positions passed, but `prev_ohlc`, production Git cleanliness/parity,
and closed-market behavior did not. VM1 was not accessed again after the final
read-only audit and no fixes, restarts, patches, commits, or deployments were
performed.
