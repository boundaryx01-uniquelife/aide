#!/bin/bash
# Server counterpart of run_heartbeat.bat: poll -> heartbeat -> morning -> evening.
# Every step runs even if an earlier one fails; the exit code is the last failure.
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="$PWD/src" PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
rc=0
for step in "poll" "heartbeat --send" "morning --send" "evening --send"; do
  python3 -m aide $step >> data/aide.log 2>&1 || rc=$?
done
exit $rc
