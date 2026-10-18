#!/usr/bin/env bash
# End-to-end checks: a real opencode binary with this plugin, against e2e/mock.ts (mock GHE + mock model).
#   OPENCODE_BIN=/path/to/opencode npm run test:e2e
# Needs Node >= 22.6 and git >= 2.32. Nothing here leaves the machine.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OC=${OPENCODE_BIN:-$(command -v opencode || true)}
[ -x "$OC" ] || { echo "opencode not found: set OPENCODE_BIN or put opencode on PATH" >&2; exit 2; }
PORT=${MOCK_PORT:-18801}
WORK=$(mktemp -d)
LOG=$WORK/requests.jsonl
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

(cd "$ROOT" && npm run build >/dev/null) || { echo "build failed" >&2; exit 1; }
node --experimental-strip-types --no-warnings "$ROOT/e2e/mock.ts" "$PORT" "$ROOT/e2e/policy.json" "$LOG" >"$WORK/mock.log" 2>&1 &
for _ in $(seq 50); do curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done
export OPENCODE_POLICY_TOKEN=test-token

failures=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n%s\n' "$1" "$2"; failures=$((failures + 1)); }
expect() { # name, haystack, needle
  if grep -qF -- "$3" <<<"$2"; then pass "$1"; else fail "$1" "    expected to find: $3
    got: $(tail -6 <<<"$2" | sed 's/^/      /')"; fi
}

# A project (a git repo on a feature branch) and an opencode home, both fresh.
setup() { # name
  local d=$WORK/$1
  mkdir -p "$d/home" "$d/repo"
  git -C "$d/repo" init -q -b feature/x
  git -C "$d/repo" config user.email dev@agency.gov
  git -C "$d/repo" config user.name "Dev"
  git -C "$d/repo" commit -q --allow-empty -m "initial"
  git -C "$d/repo" remote add origin https://github.agency.gov/team/repo.git
  cat >"$d/repo/opencode.json" <<JSON
{
  "plugin": [["file://$ROOT/dist/server.js", { "host": "http://127.0.0.1:$PORT", "repo": "platform/opencode-policy" }]],
  "model": "mock/m",
  "small_model": "mock/m",
  "share": "auto",
  "provider": {
    "mock": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Mock",
      "options": { "baseURL": "http://127.0.0.1:$PORT/v1", "apiKey": "x" },
      "models": { "m": { "name": "Mock", "tool_call": true, "limit": { "context": 100000, "output": 4000 } } }
    }
  },
  "mcp": {
    "approved": { "type": "local", "command": ["true"], "enabled": false },
    "rogue": { "type": "local", "command": ["true"], "enabled": false }
  }
}
JSON
  echo "$d"
}
oc() { # dir, args...
  local d=$1; shift
  (cd "$d/repo" && XDG_CONFIG_HOME=$d/home/config XDG_DATA_HOME=$d/home/data XDG_CACHE_HOME=$d/home/cache XDG_STATE_HOME=$d/home/state \
    timeout 90 "$OC" "$@" </dev/null 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
}
cfg() { # dir, python expression over the resolved config `c`
  oc "$1" debug config | python3 -c "import sys,json; c=json.load(sys.stdin); print($2)" 2>&1
}

echo "opencode $("$OC" --version) / git $(git --version | cut -d' ' -f3)"

d=$(setup main)
expect "config: sharing disabled despite project config" "$(cfg "$d" '(c["share"], c["autoupdate"])')" "('disabled', False)"
expect "config: unapproved MCP servers removed" "$(cfg "$d" 'sorted(c["mcp"])')" "['approved']"
expect "config: policy instructions added" "$(cfg "$d" '[i for i in c["instructions"] if "instructions-" in i]')" "ghe-policy/instructions-"

expect "guard: push to protected branch blocked" "$(oc "$d" run 'RUN: git push origin main')" \
  "Blocked by agency-baseline: pushing to protected branch 'main'"
expect "guard: force push blocked" "$(oc "$d" run 'RUN: git push --force origin feature/x')" "force-pushing is not allowed"
expect "guard: egress to unapproved host blocked" "$(oc "$d" run 'RUN: curl -s https://example.com')" "network access to 'example.com' is not allowed"
expect "guard: custom deny rule" "$(oc "$d" run 'RUN: printenv')" "dumping the environment is not allowed"
expect "guard: ordinary commands still run" "$(oc "$d" run 'RUN: echo all-good')" "Tool said: all-good"

: >"$LOG"
oc "$d" run 'RUN: git commit --allow-empty -m e2e-commit' >/dev/null
msg=$(git -C "$d/repo" log -1 --format=%B)
expect "provenance: commit keeps its message" "$msg" "e2e-commit"
expect "provenance: model trailer" "$msg" "Assisted-by: opencode (mock/m)"
expect "provenance: session trailer" "$msg" "Opencode-Session: ses_"
expect "instructions: reach the model's system prompt" "$(cat "$LOG")" "e2e marker 7f3a"

expect "status: policy_status reports the source" "$(oc "$d" run 'STATUS')" \
  "Policy: agency-baseline — from 127.0.0.1:$PORT/platform/opencode-policy/opencode-policy.json@main (blob 0123456789ab)"
expect "auth: policy fetched with the token" "$(grep '/api/v3/repos' "$LOG" | head -1)" '"auth":"Bearer test-token"'

# GHE outage: the cached copy keeps working; a machine with no cache locks down.
curl -s -m 5 -o /dev/null "http://127.0.0.1:$PORT/__mock/ghe?up=0"
out=$(oc "$d" run 'STATUS')
expect "outage: cached policy used" "$out" "Policy: agency-baseline"
expect "outage: warning shown" "$out" "using the copy cached"
d2=$(setup nocache)
expect "outage: no cache means lockdown" "$(cfg "$d2" '(c["permission"], sorted(c["mcp"]), c["share"])')" \
  "({'edit': 'ask', 'bash': 'ask', 'webfetch': 'deny'}, [], 'disabled')"
curl -s -m 5 -o /dev/null "http://127.0.0.1:$PORT/__mock/ghe?up=1"

echo
if [ "$failures" -eq 0 ]; then echo "all e2e checks passed"; else echo "$failures e2e check(s) failed"; exit 1; fi
