# Next Strategy Design: Regime-Gated Relative Value

## Decision

Spend the next research cycle on one strategy family: **market- and
sector-neutral, volatility-normalized relative value with regime gating**.

This is not an indicator-crossing strategy and it is not a relaxation of the
current approval gate. It is a better target for the existing broad-scan and
cross-sectional architecture.

## Why This Is Worth Testing

The current direct-return forest predicts absolute future returns for every
stock and produced no positive net-edge forecasts after realistic costs. That
target asks a noisy one-minute cross-section to forecast both the market move
and the stock-specific move.

The next target removes the common components first:

1. Estimate stock return relative to the benchmark and sector at the decision
   timestamp.
2. Normalize the residual by observed intraday volatility and liquidity.
3. Rank the cross-section at the same timestamp.
4. Trade only the extreme, cost-aware candidates when the market regime permits
   the relevant specialist family.

This combines established cross-sectional momentum/residual-momentum ideas with
Daybagger-specific regime, cost, portfolio, and evidence controls. It does not
assume that a published effect survives Indian intraday costs; the OOS study
must establish that.

## Predeclared Research Variants

The study may test only these variants:

- residual continuation;
- residual reversal after abnormal displacement;
- volatility expansion with relative-strength confirmation;
- regime-gated combination of the three.

No RSI, MACD, Bollinger, threshold, or model-library shopping is allowed unless
the feature has a declared economic role and improves blocked OOS evidence.

## Required Target and Evaluation

- Target: future stock return minus contemporaneous benchmark/sector return,
  with direction and volatility normalization recorded separately.
- Ranking: simultaneous cross-sectional rank at each timestamp.
- Execution: actual-size statutory costs, declared slippage, and spread
  sensitivity; no fabricated historical bid/ask.
- Portfolio: long/short candidates must be evaluated with capital-weighted P&L,
  overlap, exits, and end-of-window settlement.
- Validation: chronological OOF, untouched final time holdout, unseen-symbol
  holdout, and session/symbol block confidence intervals.

## Promotion Gates

The candidate must beat all of the following on untouched OOS data:

- no-trade baseline;
- unconditional market-direction baseline;
- simple cross-sectional rank baseline;
- the existing direct-return model;

It must also show positive net expectancy after the maximum-position cost
scenario, positive holdout rank IC, sufficient selected sessions, and no
material degradation in the unseen-symbol cohort. Failure means the strategy is
rejected and the runtime remains fail-closed.

## What Is Explicitly Deferred

Deep learning, reinforcement learning, TA-Lib indicator expansion, Backtrader,
Zipline, news-driven trading, and microstructure history are deferred. The
current data does not provide enough timestamp-safe evidence to justify them.
