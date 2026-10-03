# NEW APP: instructions for an AI agent (Claude Code on the owner's Windows PC)

The owner starts a chat with one sentence ("Build a new paper-trading app: follow NEW_APP.md in my GitHub repo
mabc1121/tt_template") and a **strategy idea**. Follow this file from top to bottom. Read `START_HERE.md` too (the map of
the template); where they differ on *how to install*, this file wins.

## Environment

* You run in Claude Code on the owner's Windows PC. Your shell tool is Git Bash; the owner's terminal is **PowerShell**.
  The AWS CLI is logged in (region ap-southeast-2). If AWS says the credentials expired, ask the owner to run `aws login`.
* Server: AWS EC2 `i-03020fa08a89fc659`, public IP `3.26.159.214`, user `ubuntu`. 2 CPUs, ~1.9 GB RAM, **no swap**.
* SSH: the key is `%TEMP%\claude\ec2key` (make it once if missing: `ssh-keygen -t ed25519 -f %TEMP%\claude\ec2key -N ""`).
  Before each connection push it (valid 60 s):
  `aws ec2-instance-connect send-ssh-public-key --instance-id i-03020fa08a89fc659 --instance-os-user ubuntu --ssh-public-key file://<path to ec2key.pub>`.
  SSH works only from the owner's current internet address.
* If SSH fails, use AWS SSM: `aws ssm send-command --instance-ids i-03020fa08a89fc659 --document-name AWS-RunShellScript ...`
  (runs as root; ~97 KB limit; copy bigger files with scp; pipe server output through `iconv -t ascii//TRANSLIT` because the
  Windows AWS CLI fails on non-ASCII text).
* UIs listen only on the server's localhost; view them through an SSH tunnel (`ssh -N -L PORT:127.0.0.1:PORT ...`).
  Use a local port that is free on the PC (other sessions keep tunnels open).
* Python on the PC: `uv run --no-project --python 3.12 ...` (add `--with-requirements config/requirements-lock.txt` for the
  template's own tests).
* **Work folder on the PC:** make a new, uniquely named folder under `%TEMP%\claude` (e.g. `%TEMP%\claude\<app-name>`).
  Other sessions work in there too: never delete or move a folder you did not create. Keep paths short: Python fails on
  the long scratchpad path (Windows MAX_PATH).
* GitHub from the PC works over HTTPS (Git Credential Manager); there is no `gh` CLI and SSH to github.com does not work.

## What is on the server (check the live state; this list may be out of date)

* Running apps, **DO NOT change** their files, services, data or keys: `tt-v2-paper` (UI 8890, the owner's main app; its
  folder `/opt/tt-v2-paper/current` is also an old checkout of this template: never pull into it), `tt-breakout` (8891),
  `tt-obi` (8892), `tt-obi-01` (8893), `tt-input` (market data service, 8900).
* Find used ports with `sudo ss -ltn` and apps with `systemctl list-units 'tt*'`. Use the next free port from 8894.
* App names: lowercase letters, digits and `-` only (the installer rejects `_`).

## How to build the app

1. **Agree a plan first.** Discuss the idea with the owner and agree a short plan (entry rule, exits, settings, long/short)
   before writing code. Ask before any big change. Useful checks before choosing thresholds: sample the live Bitfinex book
   / trades on the PC (public data, no keys) so numbers are not guesses. Costs are fixed: 5 bps fee + 1 bps slippage per
   side (~12 bps round trip).
2. **Get the latest template.** On the PC, in your work folder: `git clone https://github.com/mabc1121/tt_template.git`.
   Note the commit (`git log --oneline -1`); installs use GitHub `main`, not the server's old checkout.
3. **Write the strategy** from `examples/starter/trade.py`: only its TODO parts (settings, features, `entry_signal`,
   optional `exit_signal`; `stop_distance_bps` is the intended hook for a custom stop). Keep every PROTECTED block
   byte-for-byte. Publish your features under the platform's names where they fit (`obi_near`, `persistent_obi`,
   `tfi_fast`, `cost_bps`, `ret_15m_bps`: the Trades tab records them at each entry).
4. **Test on the PC:**
   * contract check: `python trade/validate_trade_contract.py <file> --canonical <file> --run-self-test` (the installer uses
     the file itself as the canonical reference);
   * scenario test: copy `examples/starter/scenario_test.py`, keep its tests, add a scenario for the pattern the strategy
     must catch and ones it must ignore, and one per exit rule;
   * optionally a short live smoke test on the PC with shortened warm-up settings (checks features on real data and CPU).
5. **Before any deploy, check the live state and tell the owner what will happen:** free memory (`free -m`; each app needs
   ~300 MB and there is no swap: if under ~600 MB would remain, stop and give the owner the options: add swap, stop an app,
   bigger server), load, used ports, running apps.
6. **Install from a package of GitHub main** (not from `/opt/tt-v2-paper/current`):
   * on the PC: `git -C tt_template archive --format=tar.gz -o <app>-template.tgz main`;
   * copy it and the strategy to a uniquely named folder on the server: `/tmp/<app>/`;
   * extract the package there once, only to get the installer: `mkdir /tmp/<app>/tpl && tar xzf /tmp/<app>/<app>-template.tgz -C /tmp/<app>/tpl`;
   * dry run: `cd /tmp/<app>/tpl && sudo bash deploy/install_app.sh --name <app> --port <port> --source /tmp/<app>/<app>-template.tgz --trade /tmp/<app>/<strategy>.py --market-source tt_input --dry-run`;
   * then the same without `--dry-run`, run detached (`sudo bash -c "nohup ... > /tmp/<app>/install.log 2>&1 &"`) and poll
     the log until `== installed:` or `ERROR:`; it runs the full test suite (a few minutes).
7. **API keys: the owner copies them; never type, read, print or store keys.** Give the owner ONE command to run in
   **PowerShell** (not ssh with `&&`: PowerShell 5.1 has no `&&` and Windows cannot see `/opt`):
   ```
   aws ssm send-command --instance-ids i-03020fa08a89fc659 --document-name AWS-RunShellScript --parameters "commands=install -o root -g <app> -m 640 /opt/tt-v2-paper/current/.env /opt/<app>/shared/.env && systemctl restart <app> && echo done" --query Command.CommandId --output text
   ```
   Test the format first with a harmless command (e.g. `test -f ... && echo ok`). Afterwards verify only the file's size,
   owner, mode and the service restart time: never its contents.
8. **Verify and report:** all components healthy (`/api/health`, header `Host: localhost`), market data from tt_input (a live
   connection to 127.0.0.1:8900 owned by the app's user), the strategy's decisions (Trade page Screening card), the UI
   through a tunnel (theme, Trade chart: time frames, crosshair, scrolling back; Think > Overview "Lab research" card),
   no errors in `/var/lib/<app>/logs/app.log` since the restart, the installed template commit.
9. **Lab loop:** installed off. Ask the owner to approve turning it on: the owner asks the Supervisor in the app's Manage chat
   (or you create the proposal the same way the Supervisor does) and the owner approves in Manage > Actions. Each app's Lab
   has its own daily budget; say what the total across apps becomes. Lab seats default to Claude.

## Rules

* Paper trading only. Never change PROTECTED blocks, fees, slippage or accounting.
* Never type, read, print or store API keys; never print service environments (they can contain secrets).
* Never touch other apps' files, services or data (reading their public health endpoint is fine).
* Talk to the owner before big changes. Before any deploy, check the live state and say what will happen.
* Only report numbers you actually checked, and say where they came from. Keep updates short and simple.
* If you apply a Manage action yourself (only when the owner explicitly asks): run as the app user in the app folder, put
  the app on `sys.path` inside your script (NOT `PYTHONPATH`: the test run inherits it and breaks), and use
  `ManageEngine(Path('.').resolve())`.
