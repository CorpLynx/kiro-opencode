#!/usr/bin/env bash
# Managed-policy test: settings in the system managed config (/etc/opencode/opencode.json) must win over
# everything a user controls: project config, environment variables, --pure and the test override directory.
#   sudo test/managed.sh <opencode-binary>
# Writes /etc/opencode for the duration of the run and restores whatever was there before.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OC=$1
MOCK=18921
MANAGED=/etc/opencode
[ "$(id -u)" -eq 0 ] || { echo "needs root: it writes $MANAGED" >&2; exit 2; }
W=$(mktemp -d)
BACKUP=
if [ -e "$MANAGED" ]; then BACKUP=$W/managed-backup; mv "$MANAGED" "$BACKUP"; fi
cleanup() {
  kill $(jobs -p) 2>/dev/null
  rm -rf "$MANAGED"
  [ -n "$BACKUP" ] && mv "$BACKUP" "$MANAGED"
  rm -rf "$W"
}
trap cleanup EXIT

node "$HERE/mock-model.mjs" "$MOCK" >/dev/null 2>&1 &
sleep 1

# A managed plugin that leaves a marker when it loads.
mkdir -p "$MANAGED" "$W/plugin" "$W/project" "$W/elsewhere"
cat >"$W/plugin/marker.js" <<JS
import { writeFileSync } from "node:fs"
export default { id: "managed-marker", server: async () => { writeFileSync("$W/plugin/loaded", "yes"); return {} } }
JS
cat >"$MANAGED/opencode.json" <<JSON
{ "permission": { "bash": "deny", "webfetch": "deny" },
  "share": "disabled",
  "plugin": ["file://$W/plugin/marker.js"] }
JSON
cat >"$W/project/opencode.json" <<JSON
{ "model": "mock/m", "small_model": "mock/m",
  "permission": { "bash": "allow", "webfetch": "allow" },
  "share": "auto",
  "provider": { "mock": { "npm": "@ai-sdk/openai-compatible", "name": "Mock",
    "options": { "baseURL": "http://127.0.0.1:$MOCK/v1", "apiKey": "x" },
    "models": { "m": { "name": "Mock", "tool_call": true, "limit": { "context": 100000, "output": 4000 } } } } } }
JSON
export XDG_CONFIG_HOME=$W/home/config XDG_DATA_HOME=$W/home/data XDG_CACHE_HOME=$W/home/cache XDG_STATE_HOME=$W/home/state
export OPENCODE_DISABLE_MODELS_FETCH=1
cd "$W/project"

failures=0
check() { # name, actual, expected
  if [ "$2" = "$3" ]; then printf '  PASS %s\n' "$1"; else printf '  FAIL %s\n    expected: %s\n    got:      %s\n' "$1" "$3" "$2"; failures=$((failures + 1)); fi
}
cfg() { # python expression over the resolved config `c`; extra env before it
  env "$@" timeout 60 "$OC" debug config </dev/null 2>/dev/null | python3 -c "import sys,json; c=json.load(sys.stdin); print($EXPR)" 2>&1
}

echo "opencode $("$OC" --version)"
EXPR='(c["permission"]["bash"], c["permission"]["webfetch"], c["share"])'
check "managed config beats project config" "$(cfg)" "('deny', 'deny', 'disabled')"
check "OPENCODE_PERMISSION can't loosen managed permissions" \
  "$(cfg OPENCODE_PERMISSION='{"bash":"allow","webfetch":"allow"}')" "('deny', 'deny', 'disabled')"
check "OPENCODE_TEST_MANAGED_CONFIG_DIR can't redirect managed config" \
  "$(cfg OPENCODE_TEST_MANAGED_CONFIG_DIR="$W/elsewhere")" "('deny', 'deny', 'disabled')"
EXPR='"opencode" in (c.get("disabled_providers") or [])'
check "opencode.ai's Zen provider disabled" "$(cfg)" "True"

rm -f "$W/plugin/loaded"
timeout 90 "$OC" run --pure "say hello" </dev/null >/dev/null 2>&1
check "--pure doesn't skip managed plugins" "$(cat "$W/plugin/loaded" 2>/dev/null || echo no)" "yes"
rm -f "$W/plugin/loaded"
OPENCODE_PURE=1 timeout 90 "$OC" run "say hello" </dev/null >/dev/null 2>&1
check "OPENCODE_PURE doesn't skip managed plugins" "$(cat "$W/plugin/loaded" 2>/dev/null || echo no)" "yes"

echo
if [ "$failures" -eq 0 ]; then echo "PASS: managed policy holds"; else echo "FAIL: $failures check(s)"; exit 1; fi
