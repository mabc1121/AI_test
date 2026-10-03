# research/ — the study behind this app

Everything here was produced before the app existed; nothing in it runs on the server. It is kept so every number in
the app's design can be traced.

| folder | content |
|---|---|
| `kit/` | the research kit: `research.py` (bars, labels, walk-forward models, evaluation), `config.py` (Bybit mapping), `config_bitfinex.py`, breakout / fade / cost-sensitivity scripts, the kit `.md` deliverables |
| `bitfinex/` | Bitfinex R0 recorder and replay (`bitfinex_recorder.py`, `bitfinex_replay.py`), order-level extractor (`bitfinex_l3.py`), the transfer evaluation (`transfer_eval.py`, `compare*.py`, `dayaudit.py`), the level-3 test (`l3_eval.py`, `L3_PREREG.md`) |
| `passive/` | passive-execution simulator and its kit |
| `models/` | the frozen bundles used by the app: Bybit BTCUSDT perp, 1 Jul – 23 Sep 2026, horizons 30/60/120/240 min; each holds direction, big-move and volatility models for every feature group plus the training priors |
| `reports/` | Bybit walk-forward results, the overlapping and the clean Bybit→Bitfinex transfer tests, the level-3 test, market facts, feature shift |

Headline results (details in the reports):

* Bybit, 92 days walk-forward: direction AUC ≤ 0.53 at every horizon, volatility R² 0.70–0.77, every strategy net negative at Bybit fees.
* Clean transfer to Bitfinex (models frozen 23 Sep, 9 Bitfinex days): the ALL gradient-boosting model keeps direction AUC 0.57–0.58 at 30–60 min (7–8 of 9 days above 0.5); big-move AUC 0.72–0.82; volatility R² 0.72–0.78. Price-only models fall back to 0.53. Nothing is profitable after the real spread.
* Order-level (level-3) features add nothing measurable to direction, with or without a predicted big move.

The app is the next step those reports ask for: a long, verified forward test of the frozen models on Bitfinex.

## From research to the app

| script | what it does |
|---|---|
| `bitfinex/export_models.py` | Exports the frozen bundles (ALL feature group, gradient boosting) to `trade/aitest_models.json.gz`: trees as arrays, the feature order, training priors, and the entry thresholds as quantiles of the Bybit training distribution. It checks the exported trees against scikit-learn on Bybit and Bitfinex rows. |
| `bitfinex/equivalence_test.py` | Replays the raw Bitfinex captures through the app's own `ScientificCore` and compares every minute bar, feature and prediction with the research pipeline (`bitfinex_replay.py` → `research.py bars2` → `build_features` → scikit-learn). |
| `bitfinex/live_replay_smoke.py` | Runs the real trade engine (protected R0 layer, checksum gate, broker, checkpoints, recorder, prediction log) on the raw captures at their original timestamps, including a restart with an outage. |
| `bitfinex/evaluate_live.py` | Scores the app's prediction log per horizon: direction AUC, big-move AUC, volatility R², and the signal trades' gross and net P&L, positive days and daily t at any cost. |
| `reports/app_validation/` | Results of the equivalence test and the platform replay. |
