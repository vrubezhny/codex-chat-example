#!/bin/sh
# Start/stop/login a local codex app-server instance for testing.
# Usage: codex-app-server-ctl.sh {start|stop|status|login|logout}
#
# Auth: the app-server process itself calls the OpenAI API, so credentials
# must live in ITS $CODEX_HOME (auth.json). Clients connecting to the socket
# (codex-chat.py, codex-app-server-client.py) need no login at all - the
# unix socket is local and the protocol has no auth step. Without a login,
# thread/start and turn/start still succeed but every turn fails with
# 401 Unauthorized and the thread flips to systemError status.
#
# login passes extra args straight through to `codex login`:
#   ./codex-app-server-ctl.sh login                 # browser OAuth (ChatGPT)
#   ./codex-app-server-ctl.sh login --device-auth   # print a code instead of
#                                                   # opening a browser (headless)
# Re-running login while already logged in simply re-authenticates.

CODEX_HOME="${CODEX_HOME:-/tmp/codex-app-server}"
CONTROL_DIR="$CODEX_HOME/app-server-control"
SOCK="$CONTROL_DIR/app-server-control.sock"
LOG="$CONTROL_DIR/app-server.log"

auth_status() {
    CODEX_HOME="$CODEX_HOME" codex login status 2>&1 | grep -v '^WARNING: proceeding'
}

auth_ok() {
    [ -f "$CODEX_HOME/auth.json" ]
}

start() {
    mkdir -p "$CONTROL_DIR"
    rm -f "$SOCK"
    CODEX_HOME="$CODEX_HOME" setsid nohup codex -c features.code_mode_host=true \
        app-server --listen "unix://$SOCK" >"$LOG" 2>&1 < /dev/null &
    disown
    sleep 1
    echo "Started codex app-server:"
    echo "  CODEX_HOME = $CODEX_HOME"
    echo "  socket     = $SOCK"
    echo "  log        = $LOG"
    pgrep -af -- "--listen unix://$SOCK"
    if ! auth_ok; then
        echo "WARNING: not logged in ($CODEX_HOME) - turns will fail with 401."
        echo "         Run: $0 login && $0 stop && $0 start"
    fi
}

stop() {
    if pgrep -f -- "--listen unix://$SOCK" >/dev/null 2>&1; then
        pkill -f -- "--listen unix://$SOCK"
        echo "Stopped codex app-server on $SOCK"
    else
        echo "No codex app-server running on $SOCK"
    fi
}

login() {
    echo "Logging in codex with CODEX_HOME=$CODEX_HOME"
    CODEX_HOME="$CODEX_HOME" codex login "$@"
    echo
    echo "Auth status:"
    auth_status
    if pgrep -f -- "--listen unix://$SOCK" >/dev/null 2>&1; then
        echo
        echo "NOTE: a server is already running - restart it to pick up the"
        echo "      new credentials: $0 stop && $0 start"
    fi
}

logout() {
    echo "Logging out codex (CODEX_HOME=$CODEX_HOME)"
    CODEX_HOME="$CODEX_HOME" codex logout
    auth_status
}

status() {
    pgrep -af -- "--listen unix://$SOCK" || echo "Not running ($SOCK)"
    echo -n "Auth ($CODEX_HOME): "
    if auth_ok; then
        auth_status
    else
        echo "not logged in (no $CODEX_HOME/auth.json)"
        echo "Run: $0 login && $0 stop && $0 start"
    fi
}

case "$1" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    login)  shift; login "$@" ;;
    logout) logout ;;
    *) echo "Usage: $0 {start|stop|status|login|logout}"; exit 1 ;;
esac
