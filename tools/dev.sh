#!/bin/bash
# Local dev app (host process, uvicorn --reload) running against a COPY of the
# real data. Never touches the docker deployment's database.
#
#   tools/dev.sh sync [--snapshots]  copy real data into .dev/ (stops the dev app first)
#   tools/dev.sh start               start in the background (no-op if running)
#   tools/dev.sh restart             stop + start
#   tools/dev.sh stop | status | logs [-f]
#   tools/dev.sh fg                  run in the foreground (debugger / Ctrl-C)
#
# Dev app: http://127.0.0.1:${DEV_PORT:-8002}/app   (API only: no worker, no scheduler)
# Keys (API_KEY, SETTINGS_ENCRYPTION_KEY, ...) come from .env; DATA_DIR and
# DATABASE_URL are overridden to .dev/data. The copied DB carries the runtime
# settings (LLM / iFinD endpoints), so actions that call them hit the real services.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV="$REPO/.dev"
DATA="$DEV/data"
PIDFILE="$DEV/api.pid"
LOGFILE="$DEV/api.log"
PORT="${DEV_PORT:-8002}"
cd "$REPO"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

stop() {
    if running; then
        pid="$(cat "$PIDFILE")"
        # uvicorn --reload spawns a child server process: stop it too.
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
        echo "stopped"
    fi
    rm -f "$PIDFILE"
}

run_env() {
    export DATA_DIR="$DATA"
    export DATABASE_URL="sqlite+aiosqlite:///$DATA/app.db"
    export APP_ENV=dev
    export LOG_LEVEL="${LOG_LEVEL:-DEBUG}"
    # .env points IFIND at the host loopback; the docker override uses a service name.
    export IFIND_PROXY_URL="${IFIND_PROXY_URL:-http://127.0.0.1:7100}"
}

serve_cmd() { echo .venv/bin/ia api serve --host 127.0.0.1 --port "$PORT" --reload --log-level debug; }

start() {
    [ -f "$DATA/app.db" ] || { echo "no dev data yet: run 'tools/dev.sh sync' first" >&2; exit 1; }
    if running; then echo "already running (pid $(cat "$PIDFILE")) on :$PORT"; return; fi
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "port $PORT is in use (set DEV_PORT=...)" >&2; exit 1
    fi
    run_env
    nohup $(serve_cmd) >"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"
    for _ in $(seq 1 50); do
        curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && { echo "up: http://127.0.0.1:$PORT/app (pid $(cat "$PIDFILE"), log $LOGFILE)"; return; }
        running || break
        sleep 0.3
    done
    echo "failed to start; last log lines:" >&2; tail -n 20 "$LOGFILE" >&2; exit 1
}

sync() {
    local snapshots=0
    [ "${1:-}" = "--snapshots" ] && snapshots=1
    docker info >/dev/null 2>&1 || { echo "docker/colima is not running" >&2; exit 1; }
    stop
    local stage="$DEV/data.new"
    rm -rf "$stage"; mkdir -p "$stage"
    # Online backup from inside the VM (shares the live DB's file locks). Never
    # open the volume's app.db from the host (see docker-colima-lan notes).
    docker compose run --rm --no-deps -T -v "$stage":/dev-out api python - <<'PY'
import sqlite3
src = sqlite3.connect("file:/app/db/app.db?mode=ro", uri=True)
dst = sqlite3.connect("/dev-out/app.db")
src.backup(dst)
dst.close(); src.close()
print("db snapshot ok")
PY
    for d in research-runs notifications reports outputs; do
        [ -d data/$d ] && cp -R "data/$d" "$stage/$d"
    done
    if [ $snapshots = 1 ] && [ -d data/snapshot-runs ]; then
        cp -R data/snapshot-runs "$stage/snapshot-runs"
    else
        mkdir -p "$stage/snapshot-runs"
    fi
    rm -rf "$DATA"; mv "$stage" "$DATA"
    chmod 700 "$DEV" "$DATA"
    echo "synced into $DATA ($(du -sh "$DATA" | cut -f1))"
}

case "${1:-}" in
    sync)    shift; mkdir -p "$DEV"; sync "$@" ;;
    start)   start ;;
    restart) stop; start ;;
    stop)    stop ;;
    status)  if running; then echo "running pid $(cat "$PIDFILE") on :$PORT"; else echo "stopped"; fi ;;
    logs)    if [ "${2:-}" = "-f" ]; then tail -n 100 -f "$LOGFILE"; else tail -n 100 "$LOGFILE"; fi ;;
    fg)      stop; run_env; exec $(serve_cmd) ;;
    *)       sed -n 2,14p "$0"; exit 1 ;;
esac
