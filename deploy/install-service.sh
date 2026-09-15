#!/usr/bin/env bash
#
# Install Exastro Automate as a systemd service on this Linux box.
#
#   sudo ./deploy/install-service.sh          # install, enable, start, verify
#   ./deploy/install-service.sh --dry-run     # show every decision, change nothing
#   ./deploy/install-service.sh --render      # print the unit it would install
#   sudo ./deploy/install-service.sh --remove # stop, disable, delete the unit
#
# What this script will not do, by construction:
#   * touch nginx or Apache -- no config file, no vhost, no reload, no restart.
#     The tool keeps its own port; that is what keeps somebody else's running site
#     (this box serves `office-shift.conf` on Apache) out of reach of a mistake
#     made here.
#   * change a firewall rule. ufw is *read* to tell you whether the port is
#     reachable from your LAN, and the command to open it is printed for you to
#     decide about.
#   * kill a process it cannot identify. Port 9200 being busy is only resolved by
#     stopping the app's own previous copy, matched by cwd and cmdline; anything
#     else stops the script with a message.
#
set -euo pipefail

SERVICE="exastro-automate"
UNIT="/etc/systemd/system/${SERVICE}.service"
PORT=9200

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR="$(dirname -- "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/exastro-automate.service"

MODE="install"
case "${1:-}" in
    --dry-run) MODE="dry" ;;
    --render)  MODE="render" ;;
    --remove)  MODE="remove" ;;
    "")        ;;
    *)         echo "unknown argument: $1 (use --dry-run, --render, --remove)" >&2
               exit 2 ;;
esac

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }

# ---------------------------------------------------------------- preflight
[ -f "$TEMPLATE" ] || { bad "unit template not found: $TEMPLATE"; exit 1; }
[ -f "$APP_DIR/app.py" ] || { bad "app.py not found beside deploy/: $APP_DIR"; exit 1; }

PY=""
for c in "$APP_DIR/.venv/bin/python" "$APP_DIR/../.venv/bin/python"; do
    if [ -x "$c" ]; then
        # Absolutise the path but do NOT resolve the symlink: a venv's bin/python
        # is a link to the system interpreter, and following it hands the service
        # a plain python3 with no Flask in it -- which starts, answers nothing,
        #   and dies on import. The venv finds its own prefix through that link.
        PY="$(cd -- "$(dirname -- "$c")" && pwd -P)/$(basename -- "$c")"
        break
    fi
done
[ -n "$PY" ] || { bad "no virtualenv python found in $APP_DIR/.venv or $APP_DIR/../.venv
      Create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1; }

RUN_USER="$(stat -c '%U' "$APP_DIR")"
RUN_GROUP="$(stat -c '%G' "$APP_DIR")"

# The service writes its SQLite files (creations.db, settings.db,
# flask_secret.key) into WorkingDirectory, so ownership is not decoration: a
# service running as a user who cannot write there starts, answers /healthz, and
# fails on the first run.
if [ "$(id -un)" = "$RUN_USER" ] || [ "$(id -u)" = "0" ]; then
    :
else
    bad "this script must run as root or as $RUN_USER (got $(id -un))"
    exit 1
fi

unit_rendered() {
    sed -e "s|__APP_DIR__|$APP_DIR|g" \
        -e "s|__VENV_PYTHON__|$PY|g" \
        -e "s|__RUN_USER__|$RUN_USER|g" \
        -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
        "$TEMPLATE"
}

if [ "$MODE" = "render" ]; then
    unit_rendered
    exit 0
fi

step "what will run"
say "  app directory : $APP_DIR"
say "  interpreter   : $PY"
say "  service user  : $RUN_USER:$RUN_GROUP"
say "  listen port   : $PORT (hardcoded in app.py; one worker, by design)"
say "  timezone      : $(grep -m1 '^Environment=TZ' <<<"$(unit_rendered)")"

# ------------------------------------------------------------------ the port
step "port $PORT"
HOLDER=""
if command -v ss >/dev/null 2>&1; then
    HOLDER="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$"' | head -1 || true)"
fi
if [ -z "$HOLDER" ]; then
    say "  free"
else
    say "  in use: $HOLDER"
    # Only our own previous copy may be stopped. `restart.sh` and the dev server
    # leave exactly this signature: app.py, started from this directory.
    PID="$(grep -oE 'pid=[0-9]+' <<<"$HOLDER" | head -1 | cut -d= -f2 || true)"
    OURS=""
    if [ -n "${PID:-}" ] && [ -r "/proc/$PID/cmdline" ]; then
        CMD="$(tr '\0' ' ' < "/proc/$PID/cmdline")"
        CWD="$(readlink -f "/proc/$PID/cwd" 2>/dev/null || true)"
        case "$CMD" in
            *app.py*) [ "$CWD" = "$APP_DIR" ] && OURS=1 ;;
        esac
    fi
    if [ -n "$OURS" ]; then
        if [ "$MODE" = "dry" ]; then
            say "  would stop the previous copy of this app (pid $PID, started by restart.sh or python app.py)"
        else
            say "  stopping the previous copy of this app (pid $PID)"
            kill -TERM "$PID" 2>/dev/null || true
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                kill -0 "$PID" 2>/dev/null || break
                sleep 0.5
            done
            kill -KILL "$PID" 2>/dev/null || true
        fi
    else
        bad "  something that is not this app holds port $PORT; refusing to touch it."
        say "  If that process is meant to keep the port, change app.py's port and re-run."
        exit 1
    fi
fi

# ---------------------------------------------------------------- firewall
step "reachability from your network (read-only)"
# `ufw status` needs root and says nothing useful when it cannot read the ruleset;
# the unit's own state is readable without it. The first dry run of this script
# printed "ufw is inactive" on a box where ufw was active, because a command that
# fails looks exactly like a command that reports "no".
UFW="$(systemctl is-active ufw 2>/dev/null || true)"
case "$UFW" in
    active)
        say "  ufw is ACTIVE. Whether $PORT is open from your LAN cannot be read without root:"
        say "    sudo ufw status | grep $PORT"
        say ""
        say "  If that prints nothing, clients on your network will not reach the UI, and"
        say "  the fix is a rule for the machines that need it, not an open port:"
        say "    sudo ufw allow from 192.168.0.0/24 to any port $PORT proto tcp   # your subnet"
        say ""
        say "  This form has no login. Whoever can reach the port can create movements in"
        say "  your Exastro workspace -- so allow the workstations that use it and leave it"
        say "  closed to everything else. Nothing is added by this script."
        ;;
    inactive)
        say "  ufw is inactive: the port is reachable from anything that can route here."
        say "  That is a network exposure question, not a bug -- see the note above."
        ;;
    *)
        say "  cannot tell whether a firewall is active from here; check with:"
        say "    sudo ufw status verbose"
        ;;
esac

# ------------------------------------------------------------------- apache
step "the web servers on this box (not modified)"
for svc in apache2 httpd nginx; do
    state="$(systemctl is-active "$svc" 2>/dev/null || true)"
    if [ "$state" = "active" ]; then
        say "  $svc: running, and left exactly as it is."
        say "  This service neither proxies through it nor claims :80 or :443."
    fi
done
say "  (nothing in this script edits, reloads or restarts a web server)"

# ------------------------------------------------------------------- install
if [ "$MODE" = "dry" ]; then
    step "dry run: the unit it would write to $UNIT"
    unit_rendered | sed 's/^/  /'
    step "dry run: then it would run"
    say "  systemctl daemon-reload && systemctl enable --now $SERVICE"
    say "  verify with: curl -s http://127.0.0.1:$PORT/healthz"
    exit 0
fi

if [ "$MODE" = "remove" ]; then
    step "removing $SERVICE"
    systemctl disable --now "$SERVICE" 2>/dev/null || say "  (unit was not enabled)"
    rm -f "$UNIT" && say "  deleted $UNIT"
    systemctl daemon-reload
    say "  the app files and databases are untouched."
    exit 0
fi

step "installing $UNIT"
if [ -f "$UNIT" ]; then
    if cmp -s <(unit_rendered) "$UNIT"; then
        say "  already identical to what would be written"
    else
        STAMP="$(date +%Y%m%d-%H%M%S)"
        cp -p "$UNIT" "$UNIT.bak-$STAMP"
        say "  an existing unit differs; saved as $UNIT.bak-$STAMP"
    fi
fi
TMP="$(mktemp)"
unit_rendered > "$TMP"
install -m 0644 "$TMP" "$UNIT"
rm -f "$TMP"
say "  written"

if command -v systemd-analyze >/dev/null 2>&1; then
    if ! systemd-analyze verify "$UNIT" 2>&1 | sed 's/^/  verify: /'; then
        say "  (systemd-analyze reported the above; fix it before relying on the unit)"
    fi
fi

step "enabling"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

step "checking"
ACTIVE=""
for _ in $(seq 1 20); do
    ACTIVE="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    [ "$ACTIVE" = "active" ] && break
    sleep 0.5
done
say "  systemctl is-active : ${ACTIVE:-unknown}"
if HEALTH="$(curl -s --max-time 10 "http://127.0.0.1:$PORT/healthz")"; then
    say "  /healthz            : $HEALTH"
    case "$HEALTH" in
        *'"ok": true'*|*'"ok":true'*) say "  up" ;;
        *) bad "  answered, but not with ok:true -- read the log below" ;;
    esac
else
    bad "  no answer on port $PORT -- read the log below"
fi

step "next"
say "  logs     : journalctl -u $SERVICE -f"
say "  restart  : sudo systemctl restart $SERVICE"
say "  status   : systemctl status $SERVICE --no-pager"
say ""
say "  Stop using ./restart.sh once this service is enabled: it starts a second"
say "  copy that will fail to bind port $PORT while systemd owns it."
say ""
say "  The run history and the profiles are files in $APP_DIR"
say "  (creations.db, settings.db) -- put them in your backup set, and keep"
say "  flask_secret.key stable or every /settings PIN unlock is re-entered after"
say "  a restart."
