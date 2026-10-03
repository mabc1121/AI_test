#!/usr/bin/env bash
# Install a new app from this template on this server: its own user, folders, database, Python environment,
# systemd service and UI port. Runs the health check and the full test suite before starting anything.
#
#   sudo deploy/install_app.sh --name tt-breakout --port 8891 --source /path/to/template.tgz \
#        [--trade my_trade.py] [--market-source bitfinex|tt_input] [--budget 3] [--dry-run]
#
# --source   a .tgz made with `git archive` (or a git checkout folder: its HEAD is used - never runtime files)
# --trade    a trade.py to use instead of the template's (must pass the trade contract)
# Keys: the app reads /opt/<name>/shared/.env (created empty, mode 640). Add your keys there yourself.
# Nothing existing is changed: the script stops if the name, user, folders, service or port already exist.
set -euo pipefail

NAME=""; PORT=""; SOURCE=""; TRADE=""; MARKET="bitfinex"; BUDGET="3"; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2;; --port) PORT="$2"; shift 2;; --source) SOURCE="$2"; shift 2;;
    --trade) TRADE="$2"; shift 2;; --market-source) MARKET="$2"; shift 2;; --budget) BUDGET="$2"; shift 2;;
    --dry-run) DRY=1; shift;; -h|--help) sed -n 2,13p "$0"; exit 0;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
run() { echo "+ $*"; [ "$DRY" = 1 ] || "$@"; }

# ---------------- checks (nothing is changed before all pass) ----------------
step "checks"
[ "$(id -u)" = 0 ] || fail "run as root (sudo)"
[[ "$NAME" =~ ^[a-z][a-z0-9-]{2,30}$ ]] || fail "--name: 3-31 chars, lowercase letters, digits, '-'"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || fail "--port: 1024-65535"
[[ "$BUDGET" =~ ^[0-9]+(\.[0-9]+)?$ ]] || fail "--budget: USD per day, e.g. 3"
[ "$MARKET" = bitfinex ] || [ "$MARKET" = tt_input ] || fail "--market-source: bitfinex or tt_input"
[ -n "$SOURCE" ] && [ -e "$SOURCE" ] || fail "--source: template .tgz or git checkout not found"
[ -z "$TRADE" ] || [ -f "$TRADE" ] || fail "--trade: file not found"
id "$NAME" >/dev/null 2>&1 && fail "user $NAME already exists"
[ -e "/opt/$NAME" ] && fail "/opt/$NAME already exists"
[ -e "/var/lib/$NAME" ] && fail "/var/lib/$NAME already exists"
[ -e "/etc/systemd/system/$NAME.service" ] && fail "service $NAME already exists"
ss -ltn "sport = :$PORT" | grep -q LISTEN && fail "port $PORT is already in use"
command -v python3 >/dev/null || fail "python3 missing"
python3 -c 'import sys; assert sys.version_info >= (3, 11)' || fail "python 3.11+ required"
if [ "$MARKET" = tt_input ]; then
  curl -fsS -m 3 http://127.0.0.1:8900/health >/dev/null || fail "--market-source tt_input but tt_input is not answering on 127.0.0.1:8900"
fi
echo "ok: name=$NAME port=$PORT market=$MARKET budget=\$$BUDGET/day source=$SOURCE trade=${TRADE:-template}"

TS=$(date -u +%Y%m%dT%H%M%SZ); REL="/opt/$NAME/releases/$TS"; DATA="/var/lib/$NAME"; SHARED="/opt/$NAME/shared"
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# ---------------- code ----------------
step "code"
if [ -d "$SOURCE" ]; then
  git -C "$SOURCE" -c safe.directory="$(readlink -f "$SOURCE")" archive --format=tar HEAD | tar -x -C "$WORK"
  SRC_ID="git $(git -C "$SOURCE" -c safe.directory="$(readlink -f "$SOURCE")" rev-parse --short HEAD)"
else
  tar -xzf "$SOURCE" -C "$WORK"; SRC_ID="archive $(basename "$SOURCE") sha256 $(sha256sum "$SOURCE" | cut -c1-12)"
fi
for f in core/launcher.py trade/trade.py trade/validate_trade_contract.py config/project.yaml config/requirements-lock.txt release_manifest.json; do
  [ -f "$WORK/$f" ] || fail "template is missing $f"
done
[ -e "$WORK/.env" ] && fail "template contains a .env file - refusing (secrets must never be in the template)"
[ -z "$TRADE" ] || cp "$TRADE" "$WORK/trade/trade.py"
echo "source: $SRC_ID"

# settings for this app (the template file stays JSON)
python3 - "$WORK/config/project.yaml" "$NAME" "$PORT" "$MARKET" "$BUDGET" <<'EOF'
import json, sys
p, name, port, market, budget = sys.argv[1:]
c = json.load(open(p))
c["project"]["name"] = name; c["ui"]["host"] = "127.0.0.1"; c["ui"]["port"] = int(port)
c.setdefault("market", {})["source"] = market
c.setdefault("lab", {})["budget_usd_per_day"] = float(budget)
c["lab"]["auto"] = False   # no keys yet: switch the loop on after adding them (Supervisor -> propose_lab_change, approve in Actions)
c.setdefault("agents", {}).setdefault("manage", {})["provider"] = "auto"   # the Supervisor chat uses whichever key is added (Claude first)
open(p, "w").write(json.dumps(c, indent=2) + "\n")
print("config:", {"name": name, "port": int(port), "market.source": market, "lab.budget_usd_per_day": float(budget), "lab.auto": False, "chat provider": "auto"})
EOF

# trade contract: the strategy must pass before anything is installed
python3 "$WORK/trade/validate_trade_contract.py" "$WORK/trade/trade.py" --run-self-test > "$WORK/contract.json" \
  || { cat "$WORK/contract.json"; fail "trade.py does not pass the trade contract"; }
python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print('contract: ok', r['ok'], '| self-test', r.get('self_test',{}).get('ok'), '| protected blocks', sorted(r.get('protected_hashes',{})))" "$WORK/contract.json"
echo "strategy: $(grep -m1 -oE '"name": "[^"]+"' "$WORK/trade/trade.py") | contract $(grep -m1 -oE 'TRADE_CONTRACT_VERSION = "[^"]+"' "$WORK/trade/trade.py" | cut -d'"' -f2)"
if [ "$MARKET" = tt_input ]; then
  grep -q '_run_tt_input' "$WORK/trade/trade.py" || fail "--market-source tt_input needs a contract 2.2 trade.py"
fi

# release manifest: record the hashes of the files as installed
python3 - "$WORK" <<'EOF'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1]); p = root / "release_manifest.json"; m = json.loads(p.read_text())
for f in m["files"]: m["files"][f] = hashlib.sha256((root / f).read_bytes()).hexdigest()
p.write_text(json.dumps(m, indent=2) + "\n"); print("manifest:", len(m["files"]), "files")
EOF
if [ "$DRY" = 1 ]; then step "dry run: checks, contract and config passed; nothing was installed"; exit 0; fi

# ---------------- user, folders, keys file ----------------
step "user and folders"
run useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$NAME"
run install -d -o root -g root -m 755 "/opt/$NAME" "/opt/$NAME/releases" "/opt/$NAME/backups"
run install -d -o root -g "$NAME" -m 750 "$SHARED"
run install -d -o "$NAME" -g "$NAME" -m 750 "$DATA"
[ -f "$SHARED/.env" ] || run install -o root -g "$NAME" -m 640 /dev/null "$SHARED/.env"

step "release $REL"
run mkdir -p "$REL"; run cp -a "$WORK/." "$REL/"; rm -f "$REL/contract.json"
run ln -s "$SHARED/.env" "$REL/.env"
run chown -R root:"$NAME" "$REL"
find "$REL" -type d -exec chmod 775 {} +; find "$REL" -type f -exec chmod 664 {} +   # group-writable: approved Manage edits
[ -d "$REL/deploy" ] && chmod 755 "$REL"/deploy/*.sh 2>/dev/null || true

step "python environment"
run python3 -m venv "$REL/.venv"
run "$REL/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$REL/config/requirements-lock.txt"
run chown -R root:root "$REL/.venv"; chmod -R go-w "$REL/.venv"

step "local git history (Manage records every approved change here)"
G() { sudo -u "$NAME" env HOME=/tmp GIT_CONFIG_GLOBAL=/dev/null git -c safe.directory="$REL" -C "$REL" "$@"; }
install -d -o "$NAME" -g "$NAME" -m 775 "$REL/.git"; G init -q -b main
[ -f "$REL/.gitignore" ] || fail "template .gitignore missing (it keeps .env and .venv out of git)"
G add -A
if G ls-files --error-unmatch .env >/dev/null 2>&1; then fail ".env would be committed - stopping"; fi
G -c user.name=install -c user.email="install@$NAME.local" commit -q -m "Installed $NAME from template ($SRC_ID)"
G rev-parse -q --verify HEAD >/dev/null || fail "local git history was not created"
echo "git: $(G log --oneline -1)"

# ---------------- verify before starting ----------------
step "health check and full test suite (as $NAME, on a temporary data folder)"
T=$(sudo -u "$NAME" mktemp -d); LOG="/opt/$NAME/backups/install-tests-$TS.log"
( cd "$REL" && sudo -u "$NAME" env APP_RUNTIME_DIR="$T" PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m core.doctor --full ) > "$LOG" 2>&1 \
  || { tail -40 "$LOG"; fail "health check failed - nothing started (log $LOG; remove with deploy/uninstall_app.sh $NAME)"; }
( cd "$REL" && sudo -u "$NAME" env APP_RUNTIME_DIR="$T" PYTHONDONTWRITEBYTECODE=1 nice .venv/bin/python -m tests.run_all ) >> "$LOG" 2>&1 \
  || { grep -v '^PASS' "$LOG" | tail -40; fail "tests failed - nothing started (log $LOG; remove with deploy/uninstall_app.sh $NAME)"; }
echo "health check ok; tests passed: $(grep -c '^PASS' "$LOG")"

# ---------------- service ----------------
step "service $NAME"
run ln -sfn "$REL" "/opt/$NAME/current"
cat > "/etc/systemd/system/$NAME.service" <<EOF
[Unit]
Description=$NAME paper trading research app (from template)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$NAME
Group=$NAME
WorkingDirectory=/opt/$NAME/current
ExecStart=/opt/$NAME/current/.venv/bin/python -m core.launcher
Environment=APP_RUNTIME_DIR=$DATA
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$DATA /opt/$NAME/releases
UMask=0027

[Install]
WantedBy=multi-user.target
EOF
run systemctl daemon-reload
run systemctl enable --now "$NAME"

step "waiting for all components to report healthy"
for i in $(seq 1 30); do
  sleep 3
  H=$(curl -fsS -m 3 -H "Host: localhost" "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)
  if [ -n "$H" ] && python3 -c "import json,sys; h=json.loads(sys.argv[1])['health']; sys.exit(0 if len(h)>=4 and all(v['state'] in ('HEALTHY','PAUSED') for v in h.values()) else 1)" "$H"; then
    python3 -c "import json,sys; h=json.loads(sys.argv[1])['health']; print({k:(v['state'],v['reason'][:40]) for k,v in h.items()})" "$H"
    step "installed: $NAME"
    echo "UI:        http://127.0.0.1:$PORT  (from your computer: ssh -L $PORT:127.0.0.1:$PORT ubuntu@<server>)"
    echo "next:      1) the user copies keys into $SHARED/.env, then: systemctl restart $NAME   (the chat then uses them automatically)"
    echo "           2) in the app: Manage chat -> ask the Supervisor to propose turning the Lab loop on -> approve it in Actions"
    echo "data:      $DATA     code: /opt/$NAME/current -> $REL"
    exit 0
  fi
done
echo "last health: ${H:-no answer}"; journalctl -u "$NAME" -n 30 --no-pager
fail "service started but did not become healthy within 90 s (see above)"
