# App validation

Two checks that `trade/trade.py` does what the research did, run before the app was installed anywhere.

## 1. Equivalence with the research pipeline (`equivalence_test.py`)

The 9 days of raw Bitfinex captures (24 Sep 21:21 to 3 Oct 10:59, 74.3 M book updates, 559,889 trades) were replayed
through the app's own `ScientificCore` and compared minute by minute with the research bars
(`bitfinex_replay.py` → `research.py bars2`), the research features (`build_features`) and scikit-learn predictions
of the frozen models.

| compared | columns | minutes | columns with any mismatch |
|---|---|---|---|
| minute bars | 98 | 12,339 | 0 |
| features (109 model inputs + sigma) | 110 | 12,339 | 0 |
| predictions (direction, big move, volatility × 4 horizons) | 12 | 12,339 | 0 |

The largest prediction difference is 0. Tolerance was 1e-7 relative for bars and features and 1e-6 for predictions.
The equivalence test uses the research replay's rule of carrying the book through every silent gap. Live, the app
carries gaps up to 120 s (reconnects) and leaves longer outages as empty minutes.

## 2. The platform on recorded messages (`live_replay_smoke.py`)

The real trade engine (protected R0 layer with checksum gate, strategy, paper broker, checkpoints, market recorder,
prediction log) ran on 10 hours of the raw captures at their original timestamps. At hour 7 the app was stopped, left
down for 10 minutes, and started again.

| item | result |
|---|---|
| messages processed | 3,548,255 |
| checksums verified / mismatched | 3,909 / 0 |
| health at the end | healthy |
| late checksums (> 5 s) treated as reconnects by the template's market-data layer | 7 in 10 h |
| restart | 420 minutes of history before and after; status "active" straight away, no warm-up; 6,330 recorded events refilled |
| positions | 82 executions, 39 closed trades, up to 4 open at once (one per horizon) |
| paper account | $9,971.82 from $10,000 (−$28.18); accounting valid |
| prediction log | one line per minute, every horizon |

Closed trades by horizon in those 6 trading hours (after the 4-hour warm-up) were all net negative: 30 min 17 trades,
60 min 14, 120 min 6, 240 min 2. That is consistent with the research and far too short to judge.

The recorded stream had a checksum gap over 5 s about every 80 minutes. The unchanged template market-data layer
treats each one as a reconnect. Live, the Diagnostics card ("Reconnects today") shows how often that happens.
