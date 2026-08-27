#!/usr/bin/env bash
# Keep the RJTT background monitor running.
#
# The monitor dies whenever the container is reclaimed, which a separate
# watchdog process would not survive either — it lives in the same
# container. So this is written to be called from something that already
# wakes up on its own (the 10-minute cron tick): it checks, restarts if
# needed, and says which happened.
#
# Safe to run repeatedly; it never starts a second copy.

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/rjtt_monitor.log"
PAIR_LOG="$DIR/rjtt_pairs.csv"
PIDFILE="$DIR/.monitor.pid"
INTERVAL="${1:-600}"

running() {
    [[ -f "$PIDFILE" ]] || return 1
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null)" || return 1
    [[ -n "$pid" ]] || return 1
    # Confirm the pid is ours and not some unrelated process that reused it.
    grep -q "Monitor_rjtt.py" "/proc/$pid/cmdline" 2>/dev/null
}

if running; then
    pid="$(cat "$PIDFILE")"
    if [[ ! -f "$LOG" ]]; then
        echo "monitor ALIVE (pid $pid, no log yet)"
        exit 0
    fi

    # Measure staleness from the log, but never from before we started this
    # process: a monitor launched seconds ago has not written its first line
    # yet, and judging it by the old log's mtime would kill it on sight.
    last_active=$(stat -c %Y "$LOG")
    started=$(stat -c %Y "$PIDFILE")
    (( started > last_active )) && last_active=$started

    age=$(( ($(date +%s) - last_active) / 60 ))
    if (( age <= 25 )); then
        echo "monitor ALIVE (pid $pid, log ${age} min old)"
        exit 0
    fi

    # Alive but not writing: the process is wedged, so a plain "is it running"
    # check would keep saying ALIVE forever while no data is collected.
    # Kill this specific pid (never pkill by pattern — that has matched the
    # calling shell before) and fall through to the restart below.
    echo "monitor WEDGED (pid $pid, log ${age} min old) — killing and restarting"
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    kill -9 "$pid" 2>/dev/null
    rm -f "$PIDFILE"
fi

cd "$DIR" || exit 1
nohup python3 -u Monitor_rjtt.py --interval "$INTERVAL" --pair-log "$PAIR_LOG" >> "$LOG" 2>&1 &
new_pid=$!
echo "$new_pid" > "$PIDFILE"
disown 2>/dev/null

echo "monitor RESTARTED (pid $new_pid, interval ${INTERVAL}s)"
