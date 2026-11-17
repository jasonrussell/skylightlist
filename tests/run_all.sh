#!/usr/bin/env bash
# Runs both E2E suites: lists-API (tests/mock_api.py:8765) and login/auth (tests/mock_auth.py:8766).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT
python3 "$HERE/mock_api.py" & pids+=($!)
python3 "$HERE/mock_auth.py" & pids+=($!)
sleep 1.5
r1=0; r2=0
bash "$HERE/test_api.sh"   || r1=1
bash "$HERE/test_login.sh" || r2=1
echo "OVERALL: API=$([ $r1 -eq 0 ] && echo OK || echo FAIL) LOGIN=$([ $r2 -eq 0 ] && echo OK || echo FAIL)"
[ $r1 -eq 0 ] && [ $r2 -eq 0 ]
