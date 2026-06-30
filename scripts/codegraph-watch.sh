#!/usr/bin/env bash
# CodeGraph auto-sync watcher for all quant-space projects.
# Runs `codegraph sync -q` on file changes for each project.
# Usage: ./scripts/codegraph-watch.sh start|stop|status

set -euo pipefail

PROJECTS=(
    "/home/namvt/Desktop/quant-space/system/paper-trade-system"
    "/home/namvt/Desktop/quant-space/system/market-data-service"
    "/home/namvt/Desktop/quant-space/system/trading-dashboard"
    "/home/namvt/Desktop/quant-space/system/trading-monitor"
    "/home/namvt/Desktop/quant-space/system/trading-system"
)

PID_DIR="/tmp/opencode/codegraph-watchers"
mkdir -p "$PID_DIR"

cmd="${1:-status}"

start() {
    for project in "${PROJECTS[@]}"; do
        name=$(basename "$project")
        pid_file="$PID_DIR/$name.pid"
        if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            echo "[$name] already running (pid $(cat "$pid_file"))"
            continue
        fi
        # Start inotifywait-based watcher: sync on MODIFY/CLOSE_WRITE/Create/Delete
        (
            # Requires inotifywait (inotify-tools package)
            if command -v inotifywait &>/dev/null; then
                inotifywait -m -r -q \
                    --exclude '\.(git|pyc|__pycache__|node_modules|\.codegraph|\.pytest_cache|dist|build)' \
                    -e modify -e close_write -e create -e delete \
                    "$project" 2>/dev/null | while read -r _path _event _file; do
                    # Debounce: wait 2s for batch writes, then sync once
                    sleep 2
                    codegraph sync -q "$project" 2>/dev/null || true
                    # Drain remaining events
                    while read -r -t 0.1 _; do :; done
                done
            else
                # Fallback: poll every 10s
                echo "[$name] inotifywait not found, polling every 10s"
                while true; do
                    sleep 10
                    codegraph sync -q "$project" 2>/dev/null || true
                done
            fi
        ) &
        echo $! > "$pid_file"
        echo "[$name] watcher started (pid $!)"
    done
}

stop() {
    for pid_file in "$PID_DIR"/*.pid; do
        [[ -f "$pid_file" ]] || continue
        name=$(basename "$pid_file" .pid)
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            echo "[$name] stopped (pid $pid)"
        fi
        rm -f "$pid_file"
    done
}

status() {
    for project in "${PROJECTS[@]}"; do
        name=$(basename "$project")
        pid_file="$PID_DIR/$name.pid"
        if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            echo "[$name] running (pid $(cat "$pid_file"))"
        else
            echo "[$name] stopped"
        fi
    done
}

case "$cmd" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    restart) stop; start ;;
    *) echo "Usage: $0 {start|stop|status|restart}"; exit 1 ;;
esac
