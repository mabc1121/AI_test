# Pre-registration: can Bitfinex order-level (level-3) data improve direction, especially when a big move is predicted?

Written before any confirmation-period result was looked at.

## Data
- Bitfinex tBTCUSD, 24 Sep 21:21 to 3 Oct 10:59 UTC 2026, 1-minute bars from the kit (`kit/out/bars_1m_v2.parquet`) plus
  new order-level features from `bitfinex_l3.py` (counts, ages, cancels by age, resting-order executions, sweep breadth,
  refills, walls). Level-3 features are built from each minute's own and trailing data only.
- Exploration: days 24–28 Sep (about 5,900 minutes, last H minutes purged). Confirmation: 29 Sep – 3 Oct (about 6,400 minutes).
- Horizons: 30 and 60 min. Direction label: sign of the forward log mid return over H. Big-move label: triple barrier
  (TP = SL = 1 σ_H, kit definition) hit either side.

## Conditioning ("we know there is a big move")
- Predicted: p_hit from the frozen Bybit ALL_gb hit model (trained through 23 Sep), top tercile, threshold fixed on
  exploration days. Realized (hindsight upper bound): barrier hit either side.

## Stage A, exploration only (allowed to look)
- Per-feature Spearman IC with the forward return and AUC vs direction, in all minutes, predicted-big minutes and
  realized-big minutes. Same for the level-2 counterparts already in the kit (book, depth and trade imbalances, OFI).
- Choose the three level-3 features with the largest |IC| in the predicted-big subset; fix their signs.

## Stage B, confirmation (looked at once, no changes afterwards)
- B1 features: each of the three keeps its sign, pooled AUC ≥ 0.54 in the predicted-big subset and the per-day IC has the
  chosen sign on ≥ 4 of 5 days.
- B2 models: HistGradientBoosting with the kit's parameters and seed, and L2 logistic (C = 0.1, standardised on
  exploration), trained on exploration minutes with (i) level-2 features = the kit's ALL set, (ii) ALL + level-3,
  (iii) level-3 only; also (i) and (ii) trained on predicted-big minutes only. Pass: AUC(ii) − AUC(i) ≥ +0.02 in the
  predicted-big subset on confirmation for both learners.
- B3 trading on confirmation: predicted-big minutes only, direction = sign of the model score centred on the training
  prior, enter when |score| is above the 80th percentile of exploration |score| (fixed), barrier exit TP = SL = 1 σ_H with
  time cap H, cost = actual spread at entry + 1 bp slippage, zero fees. Overlapping-trade and one-position-at-a-time
  variants. Compare (i), (ii) and the frozen Bybit ALL_gb direction score with the same selection rule. Pass: net > 0,
  daily t ≥ 2, ≥ 4 of 5 confirmation days positive.
- Placebo: level-3 features shifted by one day (same minute, previous day) in B2 must show no gain.

## What a pass would and would not mean
Five confirmation days give roughly 200 independent 30-minute windows in the predicted-big subset. A real AUC of 0.55
would often fail B1/B2 here, and B3 cannot reach t ≥ 2 unless the edge is large. Any pass is a reason to record more
Bitfinex days and repeat, not to trade.
