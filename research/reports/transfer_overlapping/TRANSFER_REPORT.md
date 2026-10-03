# Bybit → Bitfinex transfer test (no retraining)

**Question.** Do the models and strategies learned on Bybit BTCUSDT perp (92 days, 2026-07-01..09-30) transfer out-of-sample to Bitfinex tBTCUSD spot (9 days, 2026-09-24 21h..10-03 10h) without any retraining?

**Short answer.** Partly. *Skill* transfers for the tree models that use price/volatility/time features, and is in fact higher on Bitfinex. *Volatility forecasting* transfers fully. Flow-only, book-only, L2-only and logistic models do not transfer. *Economics* do not transfer into profit: with the real Bitfinex spread (median 1.6 bps round trip) plus 1 bp slippage, all-signal gross is 0–1 bps, and the handful of positive cells are concentrated, low-count and all below t = 2 on 9 days. Nothing passes.

## Protocol (what was and was not done)

- Models reconstructed deterministically from the kit (same features, hyper-parameters, seed) and fitted on all 92 Bybit days. The originals were walk-forward fold models and were not persisted; the Bybit reference numbers below are the original walk-forward out-of-sample results.
- Bitfinex used at prediction time only. No fitting, tuning, recalibration or threshold choice on Bitfinex. Confidence subsets (top 5/10/20/100 %) and vol terciles are the same grid used on Bybit, computed label-free from Bitfinex's own distribution.
- Feature mapping: the kit's bar builder was run on the Bitfinex replay, so every one of the 110 model features exists. One feature (`l2_dimb05_d15`) is 78 % NaN on Bitfinex because the ±0.5 bps band is often empty on the thin book; gradient boosting routes NaN natively, logistic maps it to the training mean. 23 of 109 features are severely distribution-shifted (spread, total depth, L2 slope/concentration/near-touch, micro-price, net adds, large-trade counts); the `shifted_feature_share` column in `compare_skill.csv` gives the share per model group. **No model was marked unavailable**, since no input is missing.
- Costs on Bitfinex: zero fees, taker both legs = actual spread at entry + 2 × 0.5 bps slippage (round trip ≈ 2.6 bps gross-to-net gap at the chosen entries). Bybit references are gross at mid (from the zero-fee sensitivity run) so the gross columns are comparable.
- Statistics: daily-sum P&L t-statistics over 9 Bitfinex days (~8.3 usable); positive days out of 9. At 240 min there are only ~50 non-overlapping windows in the whole sample, at 30 min ~400.

## 1. Direction AUC (gradient boosting), Bybit walk-forward → Bitfinex external

| H (min) | P (price/vol/time) | PL2 | ALL | L2 only | F (flow) | B (book) |
|---|---|---|---|---|---|---|
| 5 | 0.529 → 0.532 | 0.540 → 0.533 | 0.541 → 0.538 | 0.529 → 0.508 | – → 0.512 | – → 0.507 |
| 15 | 0.530 → 0.562 | 0.533 → 0.557 | 0.533 → 0.542 | 0.520 → 0.506 | 0.519 → 0.512 | 0.503 → 0.512 |
| 30 | 0.527 → 0.588 | 0.527 → 0.589 | 0.522 → 0.587 | 0.515 → 0.494 | – → 0.525 | – → 0.512 |
| 60 | 0.522 → 0.569 | 0.526 → 0.566 | 0.519 → 0.586 | 0.505 → 0.483 | 0.510 → 0.527 | 0.509 → 0.495 |
| 120 | 0.524 → 0.609 | 0.526 → 0.607 | 0.524 → 0.570 | 0.497 → 0.492 | – → 0.500 | – → 0.507 |
| 240 | 0.524 → 0.607 | 0.527 → 0.613 | 0.528 → 0.572 | 0.489 → 0.475 | 0.507 → 0.502 | 0.520 → 0.497 |

"–" = no Bybit reference at that horizon (F and B groups were only run at 15/60/240 in the v1 kit). Logistic models: P_lr rises modestly (0.53 → 0.53–0.57), every other logistic model sits at 0.48–0.52 on Bitfinex. Share of the 72 model cells with Bitfinex direction AUC > 0.52: 46 %.

**Big-move (hit) AUC**, P_gb: 0.708 → 0.723 (5), 0.694 → 0.721 (15), 0.686 → 0.724 (30), 0.705 → 0.744 (60), 0.726 → 0.778 (120), 0.745 → 0.842 (240). L2/F/B-only hit AUCs fall to 0.51–0.58.

**Per-day audit of P_gb** (`dayaudit_days.csv`): day-level AUC above 0.5 on 8/9 days at 30 min and 7/9 days at 60, 120 and 240 min. Spread of day AUCs: 0.44–0.69 at 30 min, 0.22–0.91 at 240 min. So the 30–60 min skill is broad; the 120–240 min numbers are driven by a few trend days (28 Sep −112 bps, 1 Oct +155 bps) and are not reliable with ~50 independent windows.

**What is transferring.** Raw momentum features alone have AUC below 0.5 on both venues (short-horizon mean reversion), slightly stronger on Bitfinex (`dayaudit_momentum.csv`). The tree model's nonlinear combination of return, realized-variance ratio, range position and time-of-day features is what carries over, and the thinner Bitfinex book makes those reversal patterns larger.

## 2. Volatility R² (log realized variance over the next H minutes)

| H | HGB Bybit → Bitfinex | HAR Bybit → Bitfinex |
|---|---|---|
| 5 | 0.683 → 0.716 | 0.680 → 0.703 |
| 15 | 0.767 → 0.763 | 0.767 → 0.740 |
| 30 | 0.771 → 0.774 | 0.768 → 0.742 |
| 60 | 0.768 → 0.777 | 0.758 → 0.718 |
| 120 | 0.737 → 0.772 | 0.722 → 0.658 |
| 240 | 0.691 → 0.733 | 0.662 → 0.557 |

R² on Bitfinex is measured against the Bybit training mean (no recentering). The tree vol model transfers at full strength; HAR degrades at long horizons.

## 3. P&L, fixed hold, Bybit-trained P_gb and PL2_gb on Bitfinex (gross at mid; net = gross − spread − 1 bp)

All signals (top 100 %), the honest "trade the sign" test:

| H | model | trades | win rate | gross bps | net bps | days + | t (daily) |
|---|---|---|---|---|---|---|---|
| 30 | P_gb | 11,590 | 0.56 | +1.09 | −1.55 | 4/9 | −1.32 |
| 60 | P_gb | 11,560 | 0.54 | +0.11 | −2.52 | 3/9 | −1.09 |
| 120 | P_gb | 11,500 | 0.59 | +0.61 | −2.03 | 5/9 | −0.39 |
| 240 | P_gb | 11,380 | 0.57 | −1.00 | −3.64 | 6/9 | −0.36 |

Win rate is above 50 % (consistent with the AUC) but losses are larger than wins: the model is right more often and run over on trend moves. Gross is 0–1 bps against a 2.6 bps cost.

Pre-specified headline cell, top 10 % by expected value (Bybit gross from the zero-fee sensitivity run):

| H | model | Bybit n | Bybit gross | Bybit t | Bybit folds + | Bitfinex n | Bitfinex gross | Bitfinex net | win | Bitfinex t | days + |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 | P_gb | 9,216 | +0.60 | 2.24 | 9/9 | 1,162 | +0.22 | −2.94 | 0.55 | −2.02 | 1/9 |
| 15 | P_gb | 9,215 | +1.03 | 2.40 | 9/9 | 1,161 | −0.82 | −3.92 | 0.52 | −0.95 | 4/9 |
| 60 | P_gb | 9,210 | −0.46 | −0.32 | 6/9 | 1,156 | −4.62 | −7.47 | 0.50 | −0.55 | 4/9 |
| 60 | PL2_gb | 9,210 | +0.79 | 0.54 | 6/9 | 1,156 | −0.04 | −2.92 | 0.53 | −0.23 | 5/9 |
| 240 | P_gb | 9,194 | +3.08 | 0.61 | 4/9 | 1,138 | +12.22 | +9.35 | 0.70 | 0.35 | 4/9 |
| 240 | PL2_gb | 9,192 | +4.21 | 0.85 | 5/9 | 1,138 | +18.02 | +15.16 | 0.74 | 0.57 | 3/9 |

Whole grid (6 horizons × 6 groups × 2 learners × 4 fractions, by expected value, fixed hold): 27 of 288 cells net-positive on Bitfinex, versus 38 % gross-positive. The best t among them is 1.70 (F_gb, 30 min, top 5 %, 580 trades, net +3.3 bps, 5/9 days). The largest net is PL2_gb 240 min top 5 % (+33 bps, 569 overlapping trades, 4/9 days, t 0.81). Ranking by raw score instead of expected value, 240-min top 5 % gives P_gb +21.7 bps net, 7/9 days, t 1.87, and PL2_gb +17.7, t 1.89. These are the best of ~600 cells on 9 days and at 240 min rest on a few trend days; none reaches t = 2 and the all-signal rows above show the underlying edge is thin.

Barrier exits (TP = SL = σ): every P/PL2/ALL gb cell at 5–120 min is net negative; at 240 min P_gb +4.7 and PL2_gb +6.8 bps net, t 0.4–0.6.

Vol-gated (ALL model, expected value, predicted-vol top tercile): 48 of 48 cells net negative, gross negative in most.

Sequential one-position-at-a-time, top 10 % by expected value, fixed hold (72 cells): 8 net-positive, best t 1.14 (F_gb 60 min, +4.4 bps net, 59 trades); PL2_gb 30 min +1.45 net on 84 trades, 7/9 days, t 0.31. 5-min cells are all negative with t ≈ −2 to −4.

## 4. Strategies (rules, not fitted; Bybit vol model supplies the regime forecast)

| strategy | Bybit (92 d) | Bitfinex (9 d) |
|---|---|---|
| Breakout PRIMARY (expansion tercile, d = 1σ, 2:1) | 363 trades, target hit 29 %, gross +2.6, net −12.4 at 15 bps, 0/9 folds | 54 trades, win 26 %, gross −5.2, net −8.6, 2/9 days, t −1.6 |
| Breakout unconditional 2:1 | 602 trades, gross −3.0 | 84 trades, gross −5.3, net −8.5, 1/9 days |
| Fade level tercile 1.5σ, target 1d / stop 2d | 157 trades, gross +6.3, 4/9 folds, t −0.6 (net) | 19 trades, gross −2.5, net −3.2 |
| Fade expansion tercile 1σ, target 0.5d / stop 3d | 353 trades, gross −4.7 | 70 trades, win 81 %, gross −2.3, net −2.7 |
| Fade unconditional 1.5σ, target 1d / stop 3d | 293 trades, gross +2.7, 3/9 folds | 37 trades, win 68 %, gross +6.9, net +6.4, 6/9 days, t 0.96 |

Breakout continuation is below the 33 % random-walk benchmark on both venues. Fade results are mixed and tiny on Bitfinex (19–70 trades).

## 5. Passive imbalance strategy (kit 4, same grid, no refit)

| | Bybit sample days | Bitfinex |
|---|---|---|
| PRIMARY cell (depth imbalance 0.5 bps, θ 0.5, 30 s entry, target exit 0.5 σ, 60 s) | −1.73 bps / round trip, 3,077 trips, 0/2 days | −0.55 bps, 7,637 trips, t −4.7, 0/10 days |
| Random-side baseline, same mechanics | −1.78 | −0.55 |
| Cells with positive mean P&L | 0/113 | 0/113 (best −0.35, t −3.2) |

The passive strategy equals its random-side baseline on both venues: adverse selection at fill eats the spread capture.

## 6. Verdict against the main question

1. **Direction and big-move skill transfers** for Bybit-trained gradient-boosting models on price/vol/time features (alone or with L2), and is higher on Bitfinex at 15–240 min (direction AUC 0.56–0.61 vs 0.52–0.53; hit AUC up to 0.84 vs 0.75). The 30–60 min part is broad across days; the 120–240 min part rests on two trend days.
2. **Models built on microstructure features do not transfer.** Flow-only, book-only and L2-only models are at 0.48–0.53 on Bitfinex, and the logistic versions of every group are at chance. These groups contain most of the 23 shifted features.
3. **Volatility forecasting transfers** without loss (tree R² 0.72–0.78 on Bitfinex vs 0.68–0.77 on Bybit).
4. **No strategy becomes profitable.** All-signal gross is 0–1 bps against 2.6 bps of real spread plus slippage. Of ~600 evaluated cells only a concentrated minority are net-positive, none with t ≥ 2, and the sample is 9 days. Breakout, fade and passive rules repeat their Bybit results.

Since the skill is real but small and the cost is the spread, the only economically open path on Bitfinex is one that earns the spread instead of paying it, and the passive test says the plain imbalance version of that does not work either. Any next step should be pre-registered on a longer Bitfinex recording (the recorder in kit 4 produces exactly this input) rather than tuned on these 9 days.

## Files

`compare_skill.csv`, `compare_vol.csv`, `compare_pnl.csv` (side-by-side), `transfer_skill.csv`, `transfer_pnl.csv`, `transfer_vol.csv`, `transfer_sims.csv`, `transfer_strategies.csv` (full Bitfinex grids), `dayaudit_days.csv`, `dayaudit_momentum.csv`, `feature_shift.csv`, and the scripts `transfer_eval.py`, `compare.py`, `dayaudit.py`.
