# Can Bitfinex order-level (level-3) data improve direction, especially when a big move is predicted?

**Short answer: no, not on this sample.** The order-level features carry some information about the next 30–60 minutes, but nothing a model does not already get from the level-2 book and trade flow. Adding them to the level-2 feature set moved the direction AUC in predicted-big-move minutes by +0.002 to +0.006, indistinguishable from a placebo built from the previous day's values. The three order-level features that looked strongest in the exploration days (IC 0.20–0.26) fell to 0.06–0.16 in the confirmation days, and the one directional one flipped sign. Trading on the predicted-big subset is net positive in some overlapping-trade cells, but with t ≤ 1.3 and 2–4 of 5 days positive, same with or without the order-level features.

The structural reason is in the data itself: the near-touch Bitfinex book is almost entirely fleeting. Median order age within 3 bps is 6 seconds, roughly 16,000 BTC of orders younger than 5 seconds are cancelled per day against 34 BTC of orders older than 60 seconds and about 840 BTC traded. There are 1–2 orders at the touch. Liquidity is market-maker quotes that reprice every few seconds, not patient orders whose placement, age or withdrawal reveals intent. The level-2 aggregates already capture what that quoting does.

## 1. What was built

`bitfinex_l3.py` replays the R0 feed (74.3 M order updates, 559,889 trades, 9 days) with full order ids and writes per second, per side:

- Structure: number of orders within 1, 3, 10 bps; depth within 3 and 10 bps made of orders older than 60 s and younger than 5 s; median order age; largest single resting order within 3 and 10 bps and its distance (walls); share of depth from the opening snapshot (age unknown).
- Flows within 2 bps: new orders (count), cancels (count), cancelled volume split by age of the cancelled order (< 5 s fleeting, ≥ 60 s patient), executions of resting orders, number of distinct orders and price levels executed (sweeps), refills (a new order at a price and side executed in the same second, an iceberg proxy), wall placements and pulls within 10 bps (orders ≥ 1 BTC).
- Executions are identified by volume matching: a removal or reduction at the touch is an execution only up to the volume printed on the trades channel at that side and price in the same or the previous second, allocated oldest order first. The first version of the classifier labelled every touch removal during a traded second as an execution and produced 1.8× the actual trade volume; it was replaced before anything else was changed. The corrected version attributes 6,020 of 8,411 BTC traded to identified resting orders (the rest hits hidden or already-replaced orders).

From these, 47 minute-level features: count/age/wall imbalances, old and fresh depth shares, and 1, 5, 15-minute imbalances of executions, executed order and level counts, fresh and old cancels, adds, cancels, refills and wall events, plus two intensity measures (executed orders, refills).

Order-level facts per day (medians over the 9 days):

| orders at touch | orders within 3 bps | orders within 10 bps | median age within 3 bps | largest order within 10 bps | resting executions | fresh cancels | old cancels | refills | seconds with multi-level sweeps |
|---|---|---|---|---|---|---|---|---|---|
| 1.5 | 12 | 58 | 6.1 s | 0.48 BTC | 591 BTC/day over 50,000 orders | 15,900 BTC/day | 34 BTC/day | 14,200/day | 10 % |

## 2. Protocol (pre-registered in `L3_PREREG.md` before the confirmation period was looked at)

- Exploration: 24–28 Sep (5,170 minutes at 30 min after purge). Confirmation: 29 Sep – 3 Oct (6,390 minutes). Horizons 30 and 60 min.
- Big-move condition: the frozen Bybit ALL_gb hit model (trained through 23 Sep), top tercile of its p_hit with the threshold fixed on exploration days. It selects 29–33 % of confirmation minutes and has hit AUC 0.74 there. Realized big move (barrier hit either side, hindsight) is reported as an upper bound.
- Stage A screens single features on exploration; B1 tests the three strongest order-level features on confirmation; B2 compares models trained on exploration with level-2 (the kit's 109-feature ALL set), level-2 + level-3, level-3 only and level-2 + placebo level-3 (previous day, same minute); B3 trades the predicted-big minutes on confirmation with a barrier exit at 1 σ, actual spread plus 1 bp slippage, zero fees. Learners and hyper-parameters are the kit's. Nothing was tuned on confirmation.

## 3. Results

**Stage A, exploration.** The largest |IC| in predicted-big minutes were the non-directional intensity measures (executed resting orders and refills over 5–15 min, IC −0.20 to −0.26: busier books preceded falling prices on the exploration days) and, among directional ones, 15-minute execution and sweep imbalances with a contrarian sign (IC −0.15 to −0.23). The best level-2 feature, net liquidity added within 2 bps over 5 min, had IC +0.25 (30 min) and +0.29 (60 min).

**Stage B1, confirmation, the three chosen order-level features** (sign fixed on exploration):

| H | feature | exploration IC | confirmation IC | confirmation AUC (pred-big) | days with the chosen sign | pass |
|---|---|---|---|---|---|---|
| 30 | executed orders, 15 min (−) | 0.225 | 0.056 | 0.504 | 3/5 | no |
| 30 | refills, 15 min (−) | 0.224 | 0.079 | 0.519 | 4/5 | no |
| 30 | executed orders, 5 min (−) | 0.205 | 0.066 | 0.516 | 4/5 | no |
| 60 | refills, 15 min (−) | 0.264 | 0.161 | 0.551 | 3/5 | no |
| 60 | executed orders, 15 min (−) | 0.255 | 0.101 | 0.516 | 3/5 | no |
| 60 | execution imbalance, 15 min (−) | 0.225 | −0.064 | 0.460 | 2/5 | no |

**Stage B2, models on confirmation, direction AUC (days above 0.5 in brackets):**

| H | features | learner | all minutes | predicted big | realized big |
|---|---|---|---|---|---|
| 30 | level-2 (ALL) | gb | 0.512 (3/5) | 0.520 (3/5) | 0.507 |
| 30 | level-2 + level-3 | gb | 0.517 (3/5) | 0.526 (3/5) | 0.514 |
| 30 | level-2 + placebo | gb | 0.513 | 0.517 | 0.513 |
| 30 | level-3 only | gb | 0.524 (4/5) | 0.538 (4/5) | 0.510 |
| 30 | level-2 (ALL) | lr | 0.534 (4/5) | 0.556 (4/5) | 0.539 |
| 30 | level-2 + level-3 | lr | 0.547 (5/5) | 0.558 (4/5) | 0.549 |
| 30 | level-2 + placebo | lr | 0.525 | 0.533 | 0.529 |
| 30 | level-3 only | lr | 0.543 (4/5) | 0.534 (3/5) | 0.531 |
| 30 | frozen Bybit ALL_gb | gb | 0.537 (4/5) | 0.520 (2/5) | 0.534 |
| 60 | level-2 (ALL) | gb | 0.529 (4/5) | 0.619 (4/5) | 0.549 |
| 60 | level-2 + level-3 | gb | 0.529 (4/5) | 0.621 (4/5) | 0.553 |
| 60 | level-2 + placebo | gb | 0.529 | 0.612 | 0.543 |
| 60 | level-3 only | gb | 0.489 (3/5) | 0.506 (2/5) | 0.502 |
| 60 | level-2 (ALL) | lr | 0.541 (4/5) | 0.617 (4/5) | 0.573 |
| 60 | level-2 + level-3 | lr | 0.537 (4/5) | 0.620 (4/5) | 0.572 |
| 60 | level-2 + placebo | lr | 0.535 | 0.615 | 0.565 |
| 60 | level-3 only | lr | 0.483 (2/5) | 0.527 (2/5) | 0.500 |
| 60 | frozen Bybit ALL_gb | gb | 0.542 (3/5) | 0.569 (3/5) | 0.540 |

Pre-registered criterion, ΔAUC(level-2 + level-3 − level-2) ≥ +0.02 in predicted-big minutes for both learners: 30 min +0.006 / +0.002, 60 min +0.002 / +0.003. **Fail at both horizons.** Placebo deltas: −0.003, −0.023, −0.007, −0.002. Models trained only on predicted-big minutes (in `stageB2_models.csv`) give the same picture. Level-3 alone is at chance at 60 min and 0.52–0.54 at 30 min.

**Stage B3, trading predicted-big minutes on confirmation** (barrier exit 1 σ, |score| above the exploration 80th percentile, net of actual spread + 1 bp):

| H | features | learner | mode | trades | win | gross bps | net bps | days + | t |
|---|---|---|---|---|---|---|---|---|---|
| 30 | level-2 | lr | one position | 70 | 0.56 | +2.8 | −0.3 | 3/5 | −0.09 |
| 30 | level-2 + level-3 | lr | one position | 75 | 0.69 | +8.9 | +5.7 | 4/5 | 1.25 |
| 30 | level-2 | gb | one position | 32 | 0.47 | −0.1 | −3.5 | 2/5 | −0.40 |
| 30 | level-2 + level-3 | gb | one position | 35 | 0.43 | −1.9 | −5.4 | 2/5 | −0.65 |
| 60 | level-2 | gb | overlapping | 282 | 0.67 | +25.6 | +22.4 | 4/5 | 0.92 |
| 60 | level-2 + level-3 | gb | overlapping | 295 | 0.66 | +26.1 | +22.9 | 4/5 | 0.92 |
| 60 | level-2 | gb | one position | 18 | 0.50 | −1.0 | −4.0 | 2/5 | −0.43 |
| 60 | level-2 + level-3 | gb | one position | 18 | 0.50 | +6.1 | +2.8 | 2/5 | 0.17 |
| 60 | frozen Bybit ALL_gb | gb | one position | 14 | 0.64 | −0.6 | −3.6 | 3/5 | −0.29 |

No cell passes (net > 0, t ≥ 2, ≥ 4/5 days). The level-3 additions change single cells by a few bps in both directions on 18–75 trades, which is noise.

**Post-hoc look at every feature on confirmation** (`posthoc_confirmation_features.csv`): the order-level features whose sign held in both periods are the intensity measures (more executed orders and refills in the last 5–15 min, lower return next; IC −0.08 to −0.16, 3–4 of 5 days), the share of depth within 3 bps older than 60 s (higher, return up; IC +0.09 / +0.11), the fresh-depth share (negative) and wall-pull imbalance (−0.08 to −0.10). The directional execution and sweep imbalances flipped from contrarian in exploration to momentum in confirmation. The median order-level feature has a Spearman correlation of 0.34 with its closest level-2 feature, and the execution imbalance correlates 0.73 with the trade imbalance already in the kit, which is why the models gain nothing.

## 4. What this means for "when we know there is a big move"

- With the realized big move known (hindsight), level-2 + level-3 models reach 0.55 (30 min) and 0.57 (60 min) AUC; level-3 alone 0.50–0.53. Knowing a big move is coming does not make the order-level book readable for its direction.
- In predicted-big minutes the useful signals are level-2 ones: net liquidity added near the touch over 5 min (IC +0.13 / +0.25 on confirmation) and OFI. A Bitfinex-trained level-2 model reaches 0.62 AUC in predicted-big minutes at 60 min on 4 of 5 confirmation days, versus 0.57 for the frozen Bybit model. That is a side observation from 4 training days, not a pre-registered result; it is the thing worth re-testing when more Bitfinex days exist.
- The order-level data remains valuable for execution rather than prediction: queue position, fleeting-order share and refill behaviour are exactly what a passive-fill model needs, and the earlier passive test used only level-2 proxies.

## 5. Verdict

| pre-registered test | result |
|---|---|
| B1: three strongest order-level features keep sign, AUC ≥ 0.54, ≥ 4/5 days | 0 of 6 pass |
| B2: ΔAUC ≥ +0.02 from adding order-level features, both learners | fail at 30 and 60 min (max +0.006) |
| B3: net > 0, t ≥ 2, ≥ 4/5 days in predicted-big minutes | 0 of 20 cells pass |
| placebo | indistinguishable from the real addition |

Order-level information does not improve direction prediction on Bitfinex, overall or conditional on a predicted big move, on 9 days. The book is too fleeting for order age, counts or withdrawals to carry intent beyond what the aggregated level-2 flow shows.

## Files

`bitfinex_l3.py` (extractor), `l3_eval.py` (evaluation), `L3_PREREG.md`, `l3_eval.log`, `stageA_features.csv`, `stageB1_features.csv`, `stageB2_models.csv`, `stageB3_trading.csv`, `posthoc_confirmation_features.csv`, `l3_market_facts.csv`. Per-second order-level files are in `data_bitfinex/bitfinex_l3/` (10 days, 48 columns).
