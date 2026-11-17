#!/usr/bin/env bash
# End-to-end test of skylight_lists.py against the mock server.
set -u
export SKYLIGHT_API_BASE=http://127.0.0.1:8765
export SKYLIGHT_CONFIG=/tmp/skylight-test-cfg.json
S=/home/nr4g3d/Code/skylight-lists/skylight_lists.py
pass=0; fail=0
run() { # desc, expected_exit, cmd...
  local desc="$1" exp="$2"; shift 2
  out=$("$@" 2>&1); rc=$?
  if [ "$rc" -eq "$exp" ]; then pass=$((pass+1)); echo "PASS  $desc"; else fail=$((fail+1)); echo "FAIL  $desc (exit $rc, want $exp)"; echo "$out" | sed 's/^/      |/'; fi
}

echo '{"access_token":"test-token","token_expiry":9999999999}' > /tmp/skylight-test-cfg.json

run "frames"            0 python3 $S frames
run "lists"             0 python3 $S lists
run "items by name"     0 python3 $S items Groceries
run "create list"       0 python3 $S lists --create "Shopping Run"
run "add items"         0 python3 $S add "Shopping Run" Milk Eggs --section Dairy
run "check by name"     0 python3 $S check Groceries Milk
run "uncheck by id"     0 python3 $S uncheck Groceries 5001
run "rm by id"          0 python3 $S rm Groceries 5003
run "rm --all-checked"  0 python3 $S rm Groceries --all-checked
run "add w/ section"    0 python3 $S add Groceries Butter --section Dairy
butter_id=$(python3 $S items Groceries --json | python3 -c 'import json,sys; print([i["id"] for i in json.load(sys.stdin) if i["name"]=="Butter"][0])')
run "move after"        0 python3 $S move Groceries 5001 --after "$butter_id"
run "move-section"      0 python3 $S move-section Groceries 5001 --section Dairy
run "rename list"       0 python3 $S lists --rename "Shopping Run" "Weekly Shop"
run "delete list"       0 python3 $S lists --delete "Weekly Shop"
run "api escape GET"    0 python3 $S api GET /frames/5455113/lists
run "check To-Do"       0 python3 $S check To-Do 6001
# duplicate-name ambiguity:
python3 $S add Groceries Apple Apple >/dev/null
run "ambiguous rejected" 1 python3 $S rm Groceries Apple
run "missing item"      1 python3 $S rm Groceries 99999
run "missing list"      1 python3 $S items NoSuchList
run "json output"       0 python3 $S items Groceries --json
run "frame flag after subcmd" 0 python3 $S items --frame 5455113 Groceries
# JSON:API shape normalization + live vocabulary ({id,type,attributes:{...}},
# label->name, status completed/pending -> checked bool):
if python3 -c "import sys; sys.path.insert(0,'$(dirname $S)')
from skylight_lists import unwrap
d={'data':{'list':[{'id':1,'type':'list','attributes':{'label':'Groceries','kind':'shopping','items_enabled':True}}]}}
o=unwrap(d); assert o[0]['name']=='Groceries' and o[0]['id']==1 and o[0]['items_enabled'] is True
e={'data':{'list_item':{'id':2,'type':'list_item','attributes':{'label':'Milk','status':'completed'}}}}
x=unwrap(e); assert x['name']=='Milk' and x['checked'] is True
e2={'data':{'list_item':{'id':3,'type':'list_item','attributes':{'label':'Eggs','status':'pending'}}}}
y=unwrap(e2); assert y['checked'] is False"; then
  pass=$((pass+1)); echo "PASS  json:api normalize"
else
  fail=$((fail+1)); echo "FAIL  json:api normalize"
fi
echo; echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
