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
    # Report how stale the log is, so a wedged-but-alive process is visible.
    if [[ -f "$LOG" ]]; then
        age=$(( ($(date +%s) - $(stat -c %Y "$LOG")) / 60 ))
        echo "monitor ALIVE (pid $pid, log ${age} min old)"
        if (( age > 25 )); then
            echo "  WARNING: log is stale for a ${INTERVAL}s interval — process may be wedged"
        fi
    else
        echo "monitor ALIVE (pid $pid, no log yet)"
    fi
    exit 0
fi

cd "$DIR" || exit 1
nohup python3 -u Monitor_rjtt.py --interval "$INTERVAL" --pair-log "$PAIR_LOG" >> "$LOG" 2>&1 &
new_pid=$!
echo "$new_pid" > "$PIDFILE"
disown 2>/dev/null

echo "monitor RESTARTED (pid $new_pid, interval ${INTERVAL}s)"
