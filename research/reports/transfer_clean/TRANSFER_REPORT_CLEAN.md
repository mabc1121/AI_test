# Bybit → Bitfinex transfer test, clean version (models frozen after 23 Sep 2026)

**Question.** Does the 0.56–0.59 directional AUC at 30–60 minutes still hold when the model has seen no Bybit data from the Bitfinex test period?

**Short answer.** No for the price-only models, mostly yes for the ALL model. With training cut at 23 Sep, the P and PL2 gradient-boosting models fall from 0.588/0.589 to 0.541/0.543 at 30 min and from 0.569/0.566 to 0.531/0.530 at 60 min. The ALL model (price + flow + book + L2) keeps most of its edge: 0.587 → 0.568 at 30 min and 0.586 → 0.575 at 60 min. Big-move AUC and volatility R² are unchanged. Economics are unchanged too: nothing is profitable after the Bitfinex spread and slippage.

## Protocol

- Training: Bybit BTCUSDT perp, 1 Jul 2026 00:00 to 23 Sep 2026 23:59 UTC (122,400 minutes). Test: Bitfinex tBTCUSD, 24 Sep 21h to 3 Oct 10h (9 days, ~8.3 usable). Zero calendar overlap.
- Same feature definitions, learners (HistGradientBoosting, L2 logistic, HAR), hyper-parameters, seed, training stride, horizons (5–240 min), confidence subsets (top 5/10/20/100 %), expected-value and score ranking, exits, gates and strategy rules as the previous run. The only code change is the training cutoff (`TRAIN_END=2026-09-24` in `transfer_eval.py`).
- Logistic standardisation uses Bybit-training means and standard deviations. Vol R² is measured against the Bybit-training mean. Bitfinex quantiles and vol terciles are label-free.
- No fitting, calibration, threshold choice or tuning on Bitfinex. Results are reported as they came out.
- "Prev" below is the previous run (trained through 30 Sep, overlapping 7 of the 9 Bitfinex days). "Bybit" is the original walk-forward out-of-sample result.

## 1. Direction AUC, gradient boosting: Bybit OOS | prev overlapping | clean

| H (min) | P | PL2 | ALL | L2 only | F (flow) | B (book) |
|---|---|---|---|---|---|---|
| 5 | 0.529 \| 0.532 \| **0.517** | 0.540 \| 0.533 \| **0.527** | 0.541 \| 0.538 \| **0.533** | 0.529 \| 0.508 \| 0.506 | – \| 0.512 \| 0.514 | – \| 0.507 \| 0.509 |
| 15 | 0.530 \| 0.562 \| **0.526** | 0.533 \| 0.557 \| **0.531** | 0.533 \| 0.542 \| **0.538** | 0.520 \| 0.506 \| 0.509 | 0.519 \| 0.512 \| 0.511 | 0.503 \| 0.512 \| 0.513 |
| **30** | 0.527 \| 0.588 \| **0.541** | 0.527 \| 0.589 \| **0.543** | 0.522 \| 0.587 \| **0.568** | 0.515 \| 0.494 \| 0.491 | – \| 0.525 \| 0.524 | – \| 0.512 \| 0.512 |
| **60** | 0.522 \| 0.569 \| **0.531** | 0.526 \| 0.566 \| **0.530** | 0.519 \| 0.586 \| **0.575** | 0.505 \| 0.483 \| 0.500 | 0.510 \| 0.527 \| 0.520 | 0.509 \| 0.495 \| 0.491 |
| 120 | 0.524 \| 0.609 \| **0.518** | 0.526 \| 0.607 \| **0.522** | 0.524 \| 0.570 \| **0.562** | 0.497 \| 0.492 \| 0.493 | – \| 0.500 \| 0.507 | – \| 0.507 \| 0.512 |
| 240 | 0.524 \| 0.607 \| **0.532** | 0.527 \| 0.613 \| **0.532** | 0.528 \| 0.572 \| **0.543** | 0.489 \| 0.475 \| 0.465 | 0.507 \| 0.502 \| 0.495 | 0.520 \| 0.497 \| 0.495 |

Logistic models, clean: 0.484–0.558 (P_lr 0.550 at 30 min and 0.556 at 60 min, essentially unchanged from prev 0.558/0.565; F_lr 0.553/0.550; everything else ≈ 0.50). Share of the 72 model cells above 0.52: prev 45.8 %, clean 44.4 %.

**Reading.** Seven extra Bybit training days (24–30 Sep) were worth +0.04–0.05 AUC to the price-only models on Bitfinex for the same calendar window, and +0.08–0.09 at 120–240 min. That was regime overlap through the shared BTC price path, not transfer. The ALL model loses only 0.01–0.02 at 30–60 min, so its Bitfinex edge comes mainly from features that were not time-specific. The clean price-only models land roughly where they were on Bybit itself (0.52–0.54).

**Per-day audit, clean models** (`dayaudit_days_P.csv`, `dayaudit_days_ALL.csv`):

| | P_gb 30 | P_gb 60 | ALL_gb 30 | ALL_gb 60 |
|---|---|---|---|---|
| days with AUC > 0.5 | 7/9 | 7/9 | 8/9 | 7/9 |
| median day AUC | 0.556 | 0.565 | 0.557 | 0.602 |
| range | 0.42–0.70 | 0.29–0.72 | 0.50–0.67 | 0.41–0.76 |

ALL_gb at 240 min is above 0.5 on only 3/9 days (pooled 0.543), so the long-horizon numbers are noise around a few trend days.

## 2. Big-move (hit) AUC, gradient boosting: prev | clean

| H | P | PL2 | ALL | L2 | F | B |
|---|---|---|---|---|---|---|
| 5 | 0.723 \| 0.720 | 0.721 \| 0.719 | 0.722 \| 0.720 | 0.572 \| 0.568 | 0.579 \| 0.581 | 0.519 \| 0.523 |
| 30 | 0.724 \| 0.719 | 0.724 \| 0.717 | 0.721 \| 0.713 | 0.533 \| 0.527 | 0.551 \| 0.552 | 0.512 \| 0.515 |
| 60 | 0.744 \| 0.740 | 0.744 \| 0.740 | 0.730 \| 0.722 | 0.521 \| 0.506 | 0.531 \| 0.531 | 0.514 \| 0.513 |
| 240 | 0.842 \| 0.824 | 0.842 \| 0.825 | 0.821 \| 0.803 | 0.463 \| 0.455 | 0.547 \| 0.556 | 0.509 \| 0.528 |

Bybit references: P_gb 0.686 (30), 0.705 (60), 0.745 (240). Big-move skill transfers and does not depend on the overlap.

## 3. Volatility R² (log realized variance): Bybit | prev | clean

| H | HGB | HAR |
|---|---|---|
| 5 | 0.683 \| 0.716 \| 0.715 | 0.680 \| 0.703 \| 0.702 |
| 15 | 0.767 \| 0.763 \| 0.762 | 0.767 \| 0.740 \| 0.739 |
| 30 | 0.771 \| 0.774 \| 0.772 | 0.768 \| 0.742 \| 0.741 |
| 60 | 0.768 \| 0.777 \| 0.768 | 0.758 \| 0.718 \| 0.716 |
| 120 | 0.737 \| 0.772 \| 0.765 | 0.722 \| 0.658 \| 0.655 |
| 240 | 0.691 \| 0.733 \| 0.715 | 0.662 \| 0.557 \| 0.551 |

Unchanged within 0.02.

## 4. P&L on Bitfinex (fixed hold, gross at mid, net = gross − actual spread − 1 bp slippage, zero fees), 30 and 60 min

| H | model | subset | trades | win prev → clean | gross prev → clean | net prev → clean | days+ prev → clean | t prev → clean |
|---|---|---|---|---|---|---|---|---|
| 30 | P_gb | all signals | 11,590 | 0.56 → 0.53 | +1.09 → −0.05 | −1.55 → −2.68 | 4/9 → 2/9 | −1.32 → −2.37 |
| 30 | P_gb | top 10 % score | 1,159 | 0.67 → 0.59 | +1.27 → −2.68 | −1.26 → −5.17 | 7/9 → 4/9 | −0.20 → −1.10 |
| 30 | P_gb | top 10 % EV | 1,159 | 0.56 → 0.50 | −1.38 → −4.37 | −4.27 → −7.36 | 4/9 → 5/9 | −0.66 → −1.23 |
| 30 | PL2_gb | all signals | 11,590 | 0.56 → 0.53 | +1.33 → +0.13 | −1.30 → −2.50 | 5/9 → 2/9 | −1.08 → −2.08 |
| 30 | PL2_gb | top 10 % EV | 1,159 | 0.59 → 0.50 | +1.46 → −3.62 | −1.42 → −6.63 | 7/9 → 4/9 | −0.20 → −1.00 |
| 30 | ALL_gb | all signals | 11,590 | 0.52 → 0.52 | +0.25 → +0.20 | −2.38 → −2.43 | 1/9 → 1/9 | −4.34 → −3.66 |
| 30 | ALL_gb | top 5 % score | 580 | 0.68 → 0.66 | +7.04 → +4.59 | +4.65 → +2.23 | 5/9 → 4/9 | 2.29 → 0.89 |
| 30 | ALL_gb | top 10 % score | 1,159 | 0.65 → 0.59 | +3.97 → +1.34 | +1.57 → −1.08 | 6/9 → 5/9 | 1.18 → −0.67 |
| 30 | ALL_gb | top 10 % EV | 1,159 | 0.47 → 0.44 | −5.54 → −7.40 | −8.44 → −10.38 | 3/9 → 2/9 | −1.42 → −1.78 |
| 60 | P_gb | all signals | 11,560 | 0.54 → 0.52 | +0.11 → −1.00 | −2.52 → −3.63 | 3/9 → 5/9 | −1.09 → −1.30 |
| 60 | P_gb | top 10 % score | 1,157 | 0.61 → 0.55 | +0.10 → −4.35 | −2.36 → −6.86 | 7/9 → 6/9 | −0.26 → −0.76 |
| 60 | P_gb | top 10 % EV | 1,156 | 0.50 → 0.42 | −4.62 → −11.16 | −7.47 → −14.06 | 4/9 → 3/9 | −0.55 → −1.18 |
| 60 | PL2_gb | top 10 % EV | 1,156 | 0.53 → 0.41 | −0.04 → −13.15 | −2.92 → −16.06 | 5/9 → 2/9 | −0.23 → −1.27 |
| 60 | ALL_gb | all signals | 11,560 | 0.52 → 0.52 | −0.71 → +0.05 | −3.34 → −2.58 | 1/9 → 1/9 | −2.79 → −2.12 |
| 60 | ALL_gb | top 5 % score | 582 | 0.56 → 0.64 | −6.39 → +3.52 | −8.75 → +1.00 | 6/9 → 6/9 | −0.60 → 0.22 |
| 60 | ALL_gb | top 10 % score | 1,156 | 0.62 → 0.61 | −1.43 → +1.32 | −3.90 → −1.25 | 6/9 → 5/9 | −0.41 → −0.17 |
| 60 | ALL_gb | top 10 % EV | 1,156 | 0.50 → 0.47 | −6.90 → −13.52 | −9.83 → −16.47 | 3/9 → 2/9 | −0.95 → −1.46 |

Round-trip cost at the chosen entries is 1.5–2.2 bps. The long-horizon cells that looked large before collapse: P_gb 240 min top 5 % by score goes from +21.7 bps net (t 1.87) to −11.2 bps (t −0.59); PL2_gb 240 min top 5 % by EV from +33.0 to −48.9 bps.

**Whole grid.** Fixed hold, EV ranking, 288 cells: net-positive 27 → 21; cells with t ≥ 2: 0 → 1 (F_gb 30 min top 5 %, 580 trades, +2.5 bps net, 4/9 days, t 2.01, which is 1 of 288 and was t 1.70 before). All rankings, 576 cells: 71 → 49 net-positive, 1 with t ≥ 2. Vol-gated: 0 of 96 net-positive. Sequential one-position sims: the cells that were positive before mostly flip sign (PL2_gb 30 min +1.45 → −2.67, F_gb 60 min +4.40 → −7.21, ALL_gb 60 min +1.65 → −11.28); the remaining positives have 23–98 trades and t ≤ 1.3.

## 5. Strategies (rule-based; only the vol-regime forecast comes from the frozen model)

| strategy | trades prev → clean | win | gross prev → clean | net prev → clean | days+ | t |
|---|---|---|---|---|---|---|
| Breakout PRIMARY (expansion tercile, 1σ, 2:1) | 54 → 49 | 0.26 → 0.29 | −5.2 → −8.7 | −8.6 → −12.0 | 2/9 → 2/9 | −1.6 → −2.4 |
| Breakout unconditional 2:1 | 84 → 83 | 0.26 → 0.28 | −5.3 → −4.9 | −8.5 → −8.1 | 1/9 → 1/9 | −2.0 → −2.6 |
| Fade level tercile 1.5σ, 1d/2d | 19 → 18 | 0.68 → 0.61 | −2.5 → −11.4 | −3.2 → −12.4 | 3/9 → 3/9 | −0.3 → −0.8 |
| Fade expansion tercile 1σ, 0.5d/3d | 70 → 62 | 0.81 → 0.81 | −2.3 → −3.9 | −2.7 → −4.2 | 5/9 → 6/9 | −0.6 → −0.7 |
| Fade unconditional 1.5σ, 1d/3d | 37 → 40 | 0.68 → 0.72 | +6.9 → +11.8 | +6.4 → +11.3 | 6/9 → 8/9 | 0.96 → 1.35 |

The unconditional fade is the one rule that improved; it does not use any model, has 40 trades, and was gross +2.7 bps with 3/9 folds positive on Bybit. It stays exploratory. The passive imbalance strategy has no fitted component and is unchanged (0/113 cells, equal to its random-side baseline).

## 6. Verdict: previous overlapping test vs clean non-overlapping test

| | prev (trained through 30 Sep) | clean (frozen 23 Sep) |
|---|---|---|
| P_gb direction AUC 30 / 60 min | 0.588 / 0.569 | 0.541 / 0.531 |
| PL2_gb direction AUC 30 / 60 min | 0.589 / 0.566 | 0.543 / 0.530 |
| ALL_gb direction AUC 30 / 60 min | 0.587 / 0.586 | 0.568 / 0.575 |
| P_gb direction AUC 120 / 240 min | 0.609 / 0.607 | 0.518 / 0.532 |
| P_gb big-move AUC 60 / 240 min | 0.744 / 0.842 | 0.740 / 0.824 |
| HGB vol R² 60 min | 0.777 | 0.768 |
| all-signal gross, P_gb 30 / 60 min (bps) | +1.09 / +0.11 | −0.05 / −1.00 |
| net-positive cells, fixed hold, EV | 27 / 288 | 21 / 288 |
| cells with t ≥ 2 | 0 | 1 |

1. **The 0.56–0.59 price-only direction AUC at 30–60 min does not hold.** It drops to 0.53–0.54, in line with the Bybit walk-forward level. The earlier figure was inflated by the seven overlapping days.
2. **The ALL model's 30–60 min skill largely holds** (0.568 and 0.575, 7–8 of 9 days above 0.5). It is the only directional result from the Bybit work that survives a genuinely forward external test, and it is still a 9-day sample.
3. **Big-move and volatility forecasting are unaffected** by the cutoff; both transfer.
4. **Nothing is tradable.** All-signal gross is within ±1 bp of zero against 1.5–2.6 bps of real cost; the few positive cells are 1 in several hundred, low-count, and shrink or flip relative to the overlapping run.

Models were not changed in response to any of this.

## Files

`transfer_skill.csv`, `transfer_pnl.csv`, `transfer_vol.csv`, `transfer_sims.csv`, `transfer_strategies.csv` (clean run), `compare_prev_vs_clean_{skill,vol,pnl,sims,strategies}.csv` (side by side with the previous run), `dayaudit_days_P.csv`, `dayaudit_days_ALL.csv`, `dayaudit_momentum.csv`, logs, and the scripts `transfer_eval.py` (run with `TRAIN_END=2026-09-24`), `compare_clean.py`, `dayaudit.py`.
