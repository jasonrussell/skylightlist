#!/usr/bin/env bash
# E2E test of skylight_lists.py login/refresh against tests/mock_auth.py
# (Rails/Doorkeeper-style OAuth2+PKCE flow, incl. 302 redirect chasing).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
export SKYLIGHT_API_BASE=http://127.0.0.1:8766
export SKYLIGHT_CONFIG="$HERE/.test-config.json"
S="$HERE/../skylight_lists.py"
CFG="$HERE/.test-config.json"
pass=0; fail=0
run() { # desc, expected_exit, cmd...
  local desc="$1" exp="$2"; shift 2
  out=$("$@" 2>&1); rc=$?
  if [ "$rc" -eq "$exp" ]; then pass=$((pass+1)); echo "PASS  $desc"; else fail=$((fail+1)); echo "FAIL  $desc (exit $rc, want $exp)"; echo "$out" | sed 's/^/      |/'; fi
}
check() { # desc, test-command
  local desc="$1"; shift
  if "$@" >/dev/null; then pass=$((pass+1)); echo "PASS  $desc"; else fail=$((fail+1)); echo "FAIL  $desc"; fi
}

rm -f "$CFG"
run "login (PKCE happy path)"   0 python3 "$S" login --email test@example.com --password hunter2
check "access+refresh tokens cached" python3 -c "import json,sys; c=json.load(open('$CFG')); sys.exit(0 if c['access_token'].startswith('mock-access-1') and c['refresh_token'] else 1)"
check "config file mode 600"    test "$(stat -c %a "$CFG")" = "600"
run "authenticated frames"      0 python3 "$S" frames
run "lists after login"         0 python3 "$S" lists
python3 -c "import json; c=json.load(open('$CFG')); c['token_expiry']=1; json.dump(c, open('$CFG','w'))"
run "auto-refresh expired token" 0 python3 "$S" frames
check "token rotated by refresh" python3 -c "import json,sys; c=json.load(open('$CFG')); sys.exit(0 if c['access_token'].startswith('mock-access-r') and c['token_expiry']>1000 else 1)"
run "bad creds -> friendly error" 1 python3 "$S" login --email bad@example.com --password nope
out=$(python3 "$S" login --email bad@example.com --password nope 2>&1)
check "bad creds message"       grep -q "login rejected" <<<"$out"
run "2FA re-render -> friendly error" 1 python3 "$S" login --email 2fa@example.com --password x
out=$(python3 "$S" login --email noloc@example.com --password x 2>&1); rc=$?
if [ "$rc" -eq 1 ] && ! grep -q "KeyError" <<<"$out" && grep -qi "no Location" <<<"$out"; then
  pass=$((pass+1)); echo "PASS  302-without-Location -> clean error (no KeyError)"
else
  fail=$((fail+1)); echo "FAIL  302-without-Location handling (exit $rc)"; echo "$out" | sed 's/^/      |/'
fi

rm -f "$CFG"
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
