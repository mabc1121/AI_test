# START HERE

Read this first (people and AI agents). It is the map; each file's own header has the details.

> **AI_test.** This copy of the template runs the multi-horizon forward test described in [README.md](README.md):
> `trade/trade.py` holds the strategy, `trade/aitest_models.json.gz` its frozen models, and `research/` the study behind
> it. It uses trade contract **2.3**: costs are settings and several positions can be open at once (see the header of
> `trade/trade.py`). Everything below about the platform applies unchanged.

This is a **paper-only BTCUSD trading research template**. One template, many apps: each app = this template +
its own `trade/trade.py` strategy, installed as a separate service with its own user, data, database and UI port.
Nothing here can trade real money.

## The parts

| Part | Files | What it does |
|---|---|---|
| **Trade** | `trade/trade.py`, `trade/worker.py`, `trade/state.py` | Runs the strategy (the *champion*) on live verified market data with a protected paper broker; runs *shadow* candidates next to it; checkpoints the account; records the market. |
| **Think / Lab** | `think/loop.py` (the loop), `think/scientists.py`, `think/workers.py`, `think/evaluators.py`, `think/evidence.py`, `think/replay.py`, `think/think.py` | Research in *studies*: Scientists pre-register hypotheses → Workers build exactly one `trade.py` per hypothesis (or a probe) → it runs as a shadow → Evaluators judge it against the pre-registration. Every group is 2+ independent AI seats that exchange and merge (`agents/unit.py`). |
| **Supervisor** (Manage) | `manage/supervisor.py`, `manage/manage.py` | The one agent the user talks to (Manage chat). Sees everything, sets research directives, runs the daily review. Code/config edits, restarts, Lab settings, retiring a shadow and promotions are **proposals the user approves** in Manage → Actions (validated, tested, rolled back on failure, committed to git). |
| **UI** | `ui/ui.py`, `ui/theme.json` | Private web UI on `127.0.0.1:<port>` (reach it through an SSH tunnel). Its colours and fonts come only from `ui/theme.json` (palette + rules); edit that file to change the look. |
| **Core** | `core/launcher.py`, `core/common.py`, `core/doctor.py`, `core/claude_provider.py` | Starts the four workers, config/storage/AI adapters, health check. |
| **Market data (optional, separate service)** | tt_input on `127.0.0.1:8900` | Receives, verifies, records and streams exchange data for all apps. Contract 2.2 strategies read from it; 2.1 strategies connect to Bitfinex themselves. |

Settings: `config/project.yaml` (JSON). Secrets: `.env` (never committed; see `.env.example`). Runtime data:
the folder in `APP_RUNTIME_DIR` (SQLite database = the authority for accounts, studies, actions).

## Install a new app (on the server)

**AI agents building a new app: follow `NEW_APP.md`** (environment, plan, tests, install, keys, checks, rules).

Install from a package of GitHub `main` (the server's `/opt/tt-v2-paper/current` is an older checkout: never pull into it,
it is also the main app's running code):
```bash
git -C tt_template archive --format=tar.gz -o my-app-template.tgz main     # on the PC; copy it + the strategy to /tmp/my-app/
mkdir /tmp/my-app/tpl && tar xzf /tmp/my-app/my-app-template.tgz -C /tmp/my-app/tpl && cd /tmp/my-app/tpl
sudo bash deploy/install_app.sh --name my-app --port 8894 --source /tmp/my-app/my-app-template.tgz --trade /tmp/my-app/my_trade.py --market-source tt_input --dry-run
sudo bash deploy/install_app.sh --name my-app --port 8894 --source /tmp/my-app/my-app-template.tgz --trade /tmp/my-app/my_trade.py --market-source tt_input
```
`--source` is a `git archive` .tgz or a git checkout (its committed HEAD is used - never runtime data or secrets).
Options: `--market-source bitfinex|tt_input` (tt_input needs a contract 2.2 strategy), `--budget 3` (USD/day).
Ports in use on the reference server (check `sudo ss -ltn`): 8890 (tt-v2-paper), 8891 (tt-breakout), 8892 (tt-obi),
8893 (tt-obi-01), 8900 (tt_input).

The installer refuses to touch anything that exists, validates the strategy, runs `core.doctor --full` and the full
test suite as the app's own user, and only then starts `my-app.service`. It creates `/opt/my-app/` (code, releases,
backups, `shared/.env`), `/var/lib/my-app/` (data) and a local git history. Afterwards:

**After install - checklist** (the app runs and trades without these; its AI agents need them):
1. **Keys**: the app's `/opt/my-app/shared/.env` is empty. The user copies keys in (e.g.
   `sudo install -o root -g my-app -m 640 /opt/tt-v2-paper/current/.env /opt/my-app/shared/.env`), then
   `sudo systemctl restart my-app`. Agents never type or read keys. The Supervisor chat is installed with provider
   `auto`: after the restart it uses Claude (or OpenAI if only that key is set); without keys it answers `[mock]`.
2. **Lab loop**: installed **off**. Ask the Supervisor in the app's Manage chat to propose turning it on
   (tool `propose_lab_change` with settings `{"auto": true}`), then approve it in Manage → Actions. Budget is `--budget` USD/day (default 3).
3. Remove: `sudo bash deploy/uninstall_app.sh my-app` (keeps data) or `... --purge` (deletes it). It refuses `tt-v2-paper`.

## Write a strategy (`trade/trade.py`)

**Start from `examples/starter/trade.py`** (contract 2.2: market data from tt_input). Its strategy part is laid out as
SETTINGS → DATA → STRATEGY → RISK; the comment at its top explains the input events and the output decisions.
DATA (bars, trades/volume, order book) and RISK (sizing, stop, take-profit, trailing stop, max hold, cooldown) are
ready and tested; write your settings, features and `entry_signal()` (optionally `exit_signal()`). As shipped it never trades.
Test it: `python examples/starter/scenario_test.py path/to/your_trade.py` (add your own scenario at its end).
Install it on tt_input data: `--trade path/to/your_trade.py --market-source tt_input`.

Details:

- Change **only** the two `EDITABLE` sections (`IDENTITY_AND_UI`, `SCIENTIFIC_WORKSPACE`). The four `PROTECTED`
  blocks (contract, market data, paper accounting, runtime) are checked against fixed fingerprints.
- The platform also needs these from `ScientificCore` (checked by `core.doctor`, check `strategy_interface`):
  `config` (a `@dataclass TradeConfig` with `assumed_equity_usd`), `event_history`, `position_state` (JSON-safe
  dict), `cooldown_until_ms` (int), `last_features` (dict), `_apply_event(event)` (history-only events) and
  `decide(event, positions)` (current events; must call `_apply_event` itself). The broker never closes a
  position on its own: the strategy must return `EXIT` for its stops.
- Live metrics you want in the UI: list them in `TRADE_UI_SPEC["custom_metrics"]` and put them in the decision
  `metadata`. Settings editable in the UI: `TRADE_UI_SPEC["settings"]` (TradeConfig field names).
- Check it: `python trade/validate_trade_contract.py trade/trade.py --run-self-test`, then `python -m core.doctor --full`.
  Test the logic with a scenario script before installing. Another complete example: `examples/breakout/`
  (contract 2.1, written before the starter: a range breakout with a volume filter and a trailing stop).

## Change a running app safely

- Small code/config changes: through the Supervisor (propose → user approves → validated, tested, committed,
  restarted automatically; failures roll back).
- A deploy by an operator: back up the changed files → copy them in → run the full test suite as the app user
  with a temporary `APP_RUNTIME_DIR` → roll back if it fails → update `release_manifest.json` hashes → git commit
  → restart only the affected worker (`POST /api/manage/restart {"worker": "..."}`, JSON + `Host: localhost`).
- Updating an installed app to a newer template version is **not automated yet** (do it as a deploy of the changed files).
- Before any deploy: check the live state (git status, shadow pool/slots, budget, stuck studies, health, recent
  errors) and say what the system will do first. After: check health and the loop's decision.

## Tests

`python -m tests.run_all` (39 tests: the template's 35 and the four AI_test tests in `tests/test_aitest.py`). Platform tests use the fixed reference strategy
`tests/fixtures/reference_trade.py`, so they pass whatever strategy an app runs; tests set the settings they need.
Run them with a temporary `APP_RUNTIME_DIR` so they never touch live data.

## Rules

- Paper only. In AI_test, fees and slippage are set in `PAPER_DEFAULTS` (contract 2.3). Change them, or any PROTECTED
  block, only on purpose: re-baseline the 2.3 fingerprints in `trade/validate_trade_contract.py` and run the tests.
- Secrets never in git, logs, chat or URLs.
- Promotions (a candidate replacing the champion) always need the user's approval.
- Research spends at most the daily budget (every AI call counts: studies, reviews, chat).
- Update this file when the structure changes.
