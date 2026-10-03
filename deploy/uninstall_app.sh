#!/usr/bin/env bash
# Stop and remove an app installed by install_app.sh.
#
#   sudo deploy/uninstall_app.sh NAME            stop + disable the service and remove its unit; code, data and user stay
#   sudo deploy/uninstall_app.sh NAME --purge    also delete /opt/NAME, /var/lib/NAME (database, recordings) and the user
#
# Refuses apps it did not install (the unit must say "from template") and the reference app tt-v2-paper.
set -euo pipefail
NAME="${1:-}"; PURGE="${2:-}"
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
[[ "$NAME" =~ ^[a-z][a-z0-9-]{2,30}$ ]] || { echo "usage: uninstall_app.sh NAME [--purge]" >&2; exit 2; }
[ "$NAME" != tt-v2-paper ] || { echo "refusing to remove the reference app" >&2; exit 1; }
UNIT="/etc/systemd/system/$NAME.service"
if [ -f "$UNIT" ]; then
  grep -q "from template" "$UNIT" || { echo "$UNIT was not created by install_app.sh - refusing" >&2; exit 1; }
  systemctl disable --now "$NAME" || true
  rm -f "$UNIT"; systemctl daemon-reload
  echo "service $NAME stopped and removed"
fi
if [ "$PURGE" = "--purge" ]; then
  [ -d "/opt/$NAME/releases" ] || { echo "/opt/$NAME does not look like an installed app - refusing to delete" >&2; exit 1; }
  rm -rf "/opt/$NAME" "/var/lib/$NAME"
  id "$NAME" >/dev/null 2>&1 && userdel "$NAME"
  echo "purged /opt/$NAME, /var/lib/$NAME and user $NAME"
else
  echo "kept: /opt/$NAME (code, keys), /var/lib/$NAME (data), user $NAME - add --purge to delete them"
fi
