#!/usr/bin/env bash
# End-to-end checks: drives a real opencode binary, with this plugin loaded, against e2e/mock-kiro.ts.
#   OPENCODE_BIN=/path/to/opencode npm run test:e2e
# Needs Node >= 22.6 (for --experimental-strip-types). Nothing here touches real AWS.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OC=${OPENCODE_BIN:-$(command -v opencode || true)}
[ -x "$OC" ] || { echo "opencode not found: set OPENCODE_BIN or put opencode on PATH" >&2; exit 2; }
PORT=${MOCK_PORT:-18765}
SERVE_PORT=$((PORT + 25))
WORK=$(mktemp -d)
LOG=$WORK/requests.jsonl
TMUX_SESSION=kiro-e2e-$$
trap 'tmux kill-session -t "$TMUX_SESSION" 2>/dev/null; kill $(jobs -p) 2>/dev/null; rm -rf "$WORK"' EXIT

(cd "$ROOT" && npm run build >/dev/null) || { echo "build failed" >&2; exit 1; }
node --experimental-strip-types --no-warnings "$ROOT/e2e/mock-kiro.ts" "$PORT" "$LOG" >"$WORK/mock.log" 2>&1 &
for _ in $(seq 50); do curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done

mkdir -p "$WORK/project"
cat >"$WORK/project/opencode.json" <<JSON
{ "plugin": ["file://$ROOT/dist/index.js"], "model": "kiro/claude-sonnet-4.5" }
JSON
export KIRO_API_ENDPOINT=http://127.0.0.1:$PORT KIRO_AUTH_ENDPOINT=http://127.0.0.1:$PORT

failures=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n%s\n' "$1" "$2"; failures=$((failures + 1)); }

# Each scenario gets its own opencode home, seeded with a Kiro login that expires in $2 ms.
home() {
  local d=$WORK/home-$1
  mkdir -p "$d/data/opencode"
  node -e '
    import(process.argv[1]).then(({ toStored }) => {
      const s = toStored({ accessToken: "seed-access", refreshToken: "seed-refresh", expiresAt: Date.now() + Number(process.argv[3]), authMethod: "social", region: "us-east-1" })
      require("fs").writeFileSync(process.argv[2], JSON.stringify({ kiro: { type: "oauth", ...s } }))
    })' "$ROOT/dist/auth/credentials.js" "$d/data/opencode/auth.json" "$2"
  echo "$d"
}
oc() { # home, args...
  local d=$1; shift
  (cd "$WORK/project" && XDG_CONFIG_HOME=$d/config XDG_DATA_HOME=$d/data XDG_CACHE_HOME=$d/cache XDG_STATE_HOME=$d/state \
    timeout 90 "$OC" "$@" </dev/null 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
}
expect() { # name, haystack, needle
  if grep -qF -- "$3" <<<"$2"; then pass "$1"; else fail "$1" "    expected to find: $3
    got: $(tail -5 <<<"$2" | sed 's/^/      /')"; fi
}
expect_re() { # name, haystack, extended regex
  if grep -qE -- "$3" <<<"$2"; then pass "$1"; else fail "$1" "    expected to match: $3
    got: $(tail -8 <<<"$2" | sed 's/^/      /')"; fi
}

echo "opencode $("$OC" --version) / plugin $(node -p "require('$ROOT/package.json').version")"

d=$(home models 3600000)
expect "provider registers its models" "$(oc "$d" models kiro)" "kiro/claude-sonnet-4.5"

d=$(home hello 3600000); : >"$LOG"
expect "plain chat streams text" "$(KIRO_DEBUG=1 oc "$d" run 'say hello')" "Hello from mock Kiro."
for _ in $(seq 20); do [ -s "$d/state/opencode/kiro/models.json" ] && break; sleep 0.5; done
expect "models: discovered list is offered on the next launch" "$(oc "$d" models kiro)" "kiro/claude-opus-9"
dbg=$(cat "$d/state/opencode/kiro/debug.log" 2>/dev/null)
expect "debug: request and stream are logged" "$dbg" '"event":"stream"'
if grep -q "say hello\|seed-access\|Hello from mock" <<<"$dbg"; then fail "debug: log holds no prompt, reply or token" "    found content in debug.log"; else pass "debug: log holds no prompt, reply or token"; fi

d=$(home bash 3600000); : >"$LOG"
expect "tool call round trip (bash)" "$(oc "$d" run 'please use the tool to echo hi')" "Tool said: hi"

d=$(home usage 3600000); : >"$LOG"
expect "kiro_usage tool" "$(oc "$d" run 'check my kiro usage')" "958 left"

d=$(home variant 3600000); : >"$LOG"
oc "$d" run --variant high 'say hello' >/dev/null
expect "thinking variant sets thinking mode" "$(cat "$LOG")" "<thinking_mode>enabled</thinking_mode>"

d=$(home refresh -1000); : >"$LOG"
out=$(oc "$d" run 'say hello')
expect "expired token is refreshed" "$(cat "$LOG")" '"path":"/refreshToken"'
expect "refreshed token is used" "$(cat "$LOG")" '"auth":"Bearer social-refreshed"'
expect "refreshed token is persisted" "$(node -e '
  const a = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8")).kiro
  import(process.argv[1]).then(({ fromStored }) => console.log(JSON.stringify(fromStored(a))))' \
  "$ROOT/dist/auth/credentials.js" "$d/data/opencode/auth.json")" '"refreshToken":"social-rotated"'

d=$(home error 3600000); : >"$LOG"
expect "mid-stream exception is reported" "$(oc "$d" run 'please explode')" "Mock Kiro blew up mid-stream"

d=$(home overflow 3600000); : >"$LOG"
out=$(oc "$d" run 'overflow please')
expect "context overflow triggers compaction" "$out" "conversation was compacted"
expect "session continues after compaction" "$out" "Hello from mock Kiro."

# Device login through opencode's HTTP API, starting with no credentials at all.
d=$WORK/home-login; mkdir -p "$d/data"; : >"$LOG"
(cd "$WORK/project" && XDG_CONFIG_HOME=$d/config XDG_DATA_HOME=$d/data XDG_CACHE_HOME=$d/cache XDG_STATE_HOME=$d/state exec \
  "$OC" serve --port "$SERVE_PORT" --hostname 127.0.0.1 >"$WORK/serve.log" 2>&1) &
# opencode can drop a request that arrives while it is still starting, so wait for the banner,
# then probe with a timeout until it answers.
for _ in $(seq 150); do grep -q "listening" "$WORK/serve.log" 2>/dev/null && break; sleep 0.2; done
for _ in $(seq 10); do
  [ "$(curl -s -m 30 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$SERVE_PORT/provider/auth?directory=$WORK/project")" = 200 ] && break
  sleep 1
done
api() { curl -s -m 120 -X POST -H 'content-type: application/json' -d "{\"method\":${2:-0}}" "http://127.0.0.1:$SERVE_PORT/provider/kiro/oauth/$1?directory=$WORK/project"; }
expect "login: authorize returns the verification URL" "$(api authorize)" "verify?code=ABCD-EFGH"
expect "login: callback succeeds after polling" "$(api callback)" "true"
expect "login: credentials stored with client registration" "$(node -e '
  const a = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8")).kiro
  import(process.argv[1]).then(({ fromStored }) => console.log(JSON.stringify(fromStored(a))))' \
  "$ROOT/dist/auth/credentials.js" "$d/data/opencode/auth.json" 2>&1)" '"clientSecret":"csec"'

# Google sign-in: authorize, play the browser's redirect to localhost:3128, then complete.
out=$(api authorize 2)
expect "google: authorize returns the Kiro sign-in URL" "$out" "signin_methods=google"
state=$(node -e 'const u=new URL(JSON.parse(process.argv[1]).url); console.log(u.searchParams.get("state"))' "$out" 2>/dev/null)
( sleep 1; curl -s -m 10 -o /dev/null "http://localhost:3128/oauth/callback?login_option=google&code=abc&state=$state" ) &
expect "google: callback succeeds after the redirect" "$(api callback 2)" "true"
expect "google: social session stored" "$(node -e '
  const a = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8")).kiro
  import(process.argv[1]).then(({ fromStored }) => console.log(JSON.stringify(fromStored(a))))' \
  "$ROOT/dist/auth/credentials.js" "$d/data/opencode/auth.json" 2>&1)" '"accessToken":"social-access"'

# The TUI tracker, in a real opencode TUI inside tmux. Uses the `./server` + `./tui` entries.
if command -v tmux >/dev/null; then
  d=$(home tui 3600000)
  mkdir -p "$d/config/opencode" "$WORK/tui-project"
  echo "{ \"plugin\": [\"file://$ROOT/dist/tui.js\"] }" >"$d/config/opencode/tui.json"
  echo "{ \"plugin\": [[\"file://$ROOT/dist/server.js\", { \"accountRefreshSeconds\": 0 }]], \"model\": \"kiro/claude-sonnet-4.5\" }" >"$WORK/tui-project/opencode.json"
  tmux new-session -d -s "$TMUX_SESSION" -x 200 -y 50 -c "$WORK/tui-project" \
    "env XDG_CONFIG_HOME=$d/config XDG_DATA_HOME=$d/data XDG_CACHE_HOME=$d/cache XDG_STATE_HOME=$d/state \
     KIRO_API_ENDPOINT=$KIRO_API_ENDPOINT KIRO_AUTH_ENDPOINT=$KIRO_AUTH_ENDPOINT TERM=xterm-256color '$OC'; sleep 300"
  screen() { tmux capture-pane -p -t "$TMUX_SESSION"; }
  waitfor() { for _ in $(seq "$2"); do screen | grep -qE "$1" && return 0; sleep 1; done; return 1; }
  waitfor "Ask anything" 90
  tmux send-keys -t "$TMUX_SESSION" "say hello"; sleep 0.5; tmux send-keys -t "$TMUX_SESSION" Enter
  waitfor "credits · [0-9]+ request" 90
  sleep 2
  out=$(screen)
  expect_re "tui: sidebar context bar" "$out" "[█░]+ [0-9]+% context"
  expect_re "tui: sidebar session credits" "$out" "[0-9.]+ credits · [0-9]+ requests?"
  expect "tui: sidebar account credits" "$out" "958 / 1,000 left on account"
  expect_re "tui: prompt-row usage" "$out" "[0-9]+% · [0-9.]+ cr"

  # Low balance: the next account refresh should raise a warning toast.
  curl -s -m 5 -o /dev/null "$KIRO_API_ENDPOINT/__mock/credits?used=950"
  tmux send-keys -t "$TMUX_SESSION" "say hello again"; sleep 0.5; tmux send-keys -t "$TMUX_SESSION" Enter
  waitfor "credits running low" 90
  expect "tui: low-credit warning" "$(screen)" "50 of 1,000 credits left on your Kiro account."

  # /kiro opens the usage dialog.
  tmux send-keys -t "$TMUX_SESSION" "/kiro"; sleep 1.5; tmux send-keys -t "$TMUX_SESSION" Enter
  waitfor "This session" 20
  out=$(screen)
  expect "tui: /kiro dialog" "$out" "Kiro usage"
  expect_re "tui: /kiro dialog figures" "$out" "Credits +[0-9.]+ over [0-9]+ requests"
  curl -s -m 5 -o /dev/null "$KIRO_API_ENDPOINT/__mock/credits?used=42"
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null
else
  echo "  SKIP tui tracker (tmux not installed)"
fi

echo
if [ "$failures" -eq 0 ]; then echo "all e2e checks passed"; else echo "$failures e2e check(s) failed"; exit 1; fi
