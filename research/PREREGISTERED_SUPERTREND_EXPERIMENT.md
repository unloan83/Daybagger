# Preregistered Supertrend+ATR Experiment

Status: fixed before implementation.

## Hypothesis

A single Supertrend-style ATR trend family can capture directional persistence and volatility expansion that the rejected v4 direct-return stack did not express cleanly. The indicator is used only to generate a directional candidate; it is not combined with RSI, MACD, Bollinger Bands, volume filters, or learned thresholds.

## Fixed Parameters

- Fresh window: `2026-01-05` through `2026-03-31`.
- ATR lookback: 14 one-minute bars.
- Supertrend multiplier: 3.0.
- Decision stride: 15 minutes.
- Warmup: 30 bars.
- Triple barrier: stop = 1 ATR, target = 2 ATR, time exit = 15 minutes.
- Cost hurdle: realistic maximum-position statutory cost plus declared two-sided paper slippage.
- No parameter search or post-result tuning.

## Pass Bar

All must pass on the fresh window:

- At least 20 positive-edge rows after costs.
- Mean realised net return at least +10 bps.
- Spearman rank IC at least +0.05 between fixed signal score and realised net return.

## Kill Condition

Failure of any one bar kills this candidate family. No threshold tuning, filter stacking, indicator combinations, or parameter rescue is permitted.

## Safety

This is research-only and does not create or promote `config/validated_meta_model.json`. The Daybagger runtime remains fail-closed.
