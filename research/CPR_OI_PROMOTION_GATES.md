# CPR/OI Strategy Promotion Gates

Status: **LOCKED BEFORE CPR/OI BACKTEST OR VARIANT RESULTS**  
Locked at: 2026-09-11 11:16 UTC  
Source commit at lock time: `8b8e9841df5ea3f0141a0261caed48eee93e1c64`

These thresholds apply to the active CPR/OI + 1.5R strategy, its proposed
entry-timing variants, the untouched historical holdout, and the subsequent
forward-paper corroboration period. They must not be relaxed after results are
observed. A failure is reported as **not yet provable**, not addressed by
changing the threshold.

## Minimum sample

- At least **400 clean, non-duplicated qualifying trades** in the historical
  holdout.
- Trades must span at least **60 distinct trading days**.
- A clean trade is one unique accepted signal/position produced after all P0
  safeguards, with no duplicate instrument position and no geometry fault.
- At a 50% win rate, which maximizes Bernoulli variance, 400 independent trades
  give an approximate 95% margin of error of 4.9 percentage points:
  `1.96 * sqrt(0.5 * 0.5 / 400) = 0.049`.
- Because same-day and same-sector trades are not independent, statistical
  confidence intervals must use resampling clustered by trading day. The
  400-trade count is a minimum observation count, not permission to treat every
  row as independent.

## Historical holdout promotion thresholds

All conditions must pass on net returns after the canonical cost and slippage
model:

1. Holdout trade count >= 400 and distinct trading days >= 60.
2. Holdout profit factor >= **1.25**.
3. Portfolio maximum drawdown <= **15.0%** at the intended production risk and
   exposure limits.
4. Mean net expectancy > 0 and the lower bound of a **two-sided 95% confidence
   interval** for mean net expectancy is strictly > 0.
5. The confidence interval must be generated using a reproducible bootstrap
   clustered by trading day with a fixed, recorded random seed and at least
   10,000 resamples. This corresponds to significance level alpha = 0.05.

No rounded value may be used to convert a failure into a pass.

## Variant selection discipline

- Strategy parameters and candidate variants are selected using train data.
- A single choice may be made using validation data.
- After holdout is opened, there is no further tuning, reselection, threshold
  adjustment, or replacement of the holdout.
- Entry-timing or target-feasibility changes are promoted only if the selected
  variant passes every historical holdout threshold above.

## Forward-paper corroboration

- Minimum four calendar weeks and at least 400 clean qualifying trades,
  whichever takes longer.
- No strategy parameter changes are permitted. Any strategy parameter change
  resets both the calendar and trade-count requirements.
- Forward paper must independently meet profit factor >= 1.25, maximum drawdown
  <= 15.0%, and a day-clustered 95% confidence-interval lower bound for mean net
  expectancy > 0.

## Live-money gate

Live money is not eligible unless the untouched historical holdout and the
untouched forward-paper period both pass their gates. Broker contract-note
reconciliation remains a separate live-stage checklist item and cannot be
claimed complete from modeled paper costs alone.
