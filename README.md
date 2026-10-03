# AI_test — multi-horizon forward test on live Bitfinex data

AI_test is an app built on the [tt_template](https://github.com/mabc1121/tt_template) paper-trading research
platform. It runs the frozen models from the research in [`research/`](research/README.md) on live Bitfinex
tBTCUSD data, at four horizons at once, as a paper forward test. Nothing here can trade real money.

**Read [START_HERE.md](START_HERE.md) first** for the platform map: Trade, Think / Lab, Supervisor, UI, install.

## What the strategy does

| part | what it is |
|---|---|
| Data | Verified R0 order book and public trades, turned into one bar per minute exactly as the research did. |
| Features | The 109 research features (price, volatility, trade flow, book, full L2), recomputed every minute. |
| Models | For each horizon, a direction, a big-move and a volatility model, trained on Bybit BTCUSDT from 1 Jul to 23 Sep 2026 and never on Bitfinex. They are stored as plain arrays in `trade/aitest_models.json.gz`. |
| Slots | One slot each for 30, 60, 120 and 240 minutes. A slot enters when the big-move probability is in its top tercile and the direction score is in its top 20 %, both measured on the Bybit training data. It exits at a symmetric barrier of 1 × sigma × √H, or after H minutes. |
| Positions | Slots trade independently, so up to four positions can be open at once. Raise *Max Positions / Horizon* for overlapping entries. |
| Costs | Zero fee plus 0.5 bps slippage per side, on top of crossing the real spread. They are set in `PAPER_DEFAULTS` in `trade/trade.py`. |
| Warm-up | On a fresh install the app trades after 4 hours, and features reach full research quality after 24 hours. |
| Restarts | The minute history, the book and the open minute are saved every 5 minutes. Downtime is refilled from the app's own market recordings, so a restart needs no warm-up. |
| Log | Every minute's predictions for every horizon go to `<runtime>/aitest_predictions/`. Run `research/bitfinex/evaluate_live.py` on that folder for AUC, hit rate and P&L per horizon at any cost. |

The research predicts this paper account will lose money. The best direction AUC on Bitfinex was 0.57–0.58 at
30–60 minutes, and the gross edge is smaller than the spread. The app exists to measure, over weeks of verified
live data, whether that skill holds at each horizon. Its value is the prediction log and the per-horizon trade
statistics, not the paper P&L.

## What differs from the template

* **Contract 2.3.** Costs are settings, and the broker keys positions by `position_id`, so several can be open at
  once. `TradeRuntime.initialize()` calls the strategy's `on_runtime_initialize()`. The 2.3 fingerprints are in
  `trade/validate_trade_contract.py`. The market-data block is byte-identical to the template's 2.2, so tt_input
  works as before.
* Trade pairing for the Trades page and Lab statistics matches entries and exits by `position_id`. The setup is the
  horizon slot, for example `H60`.
* `tests/test_aitest.py` has four extra tests: model arrays against scikit-learn, the broker's costs and concurrent
  positions, the slots' entries and exits, and a restart without warm-up. The suite now has 39 tests.

To edit a protected block on purpose, change it, re-baseline the four 2.3 hashes in
`trade/validate_trade_contract.py`, and run `python -m tests.run_all`.

## Install on the server

Install it like any template app (see NEW_APP.md for the full checklist). Do it from a package of this repository's
branch, and leave out `--trade`, because the strategy is already `trade/trade.py`:

```bash
git -C AI_test archive --format=tar.gz -o ai-test.tgz claude/cool-clarke-qkt1xc     # on the PC
# copy ai-test.tgz to /tmp/ai-test/ on the server, then:
mkdir /tmp/ai-test/tpl && tar xzf /tmp/ai-test/ai-test.tgz -C /tmp/ai-test/tpl && cd /tmp/ai-test/tpl
sudo bash deploy/install_app.sh --name ai-test --port 8894 --source /tmp/ai-test/ai-test.tgz --market-source tt_input --dry-run
sudo bash deploy/install_app.sh --name ai-test --port 8894 --source /tmp/ai-test/ai-test.tgz --market-source tt_input
```

Check free memory first. The server has no swap, and each app needs about 300 MB. Use the next free port. After a
week, copy `/var/lib/ai-test/aitest_predictions/` to the PC and run:

```bash
python research/bitfinex/evaluate_live.py --log aitest_predictions --costs 0 1 2 3 --out live_eval
```
