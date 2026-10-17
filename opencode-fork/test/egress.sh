#!/usr/bin/env bash
# Egress test: drives an opencode binary through everyday use, and through attempts to make it call out,
# and records every outbound connection attempt at two levels:
#   * a proxy (HTTPS_PROXY) that logs the host each proxy-aware client asks for, and refuses it;
#   * strace on every connect() syscall, which also catches clients that ignore the proxy, and DNS lookups.
# A hardened build must reach nothing: the only connections allowed are to the local proxy and mock model.
#   test/egress.sh <opencode-binary> [--expect-none]
# Needs node, python3, strace and curl; tmux for the TUI scenario.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OC=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
EXPECT_NONE=${2:-}
PROXY=18910 MOCK=18911 SERVE=18912
W=$(mktemp -d)
LOG=$W/proxy.jsonl
: >"$LOG"
cleanup() { tmux kill-session -t fork-egress 2>/dev/null; kill $(jobs -p) 2>/dev/null; wait 2>/dev/null; rm -rf "$W"; }
trap cleanup EXIT

node "$HERE/egress-recorder.mjs" "$PROXY" "$LOG" >/dev/null 2>&1 &
node "$HERE/mock-model.mjs" "$MOCK" >/dev/null 2>&1 &
sleep 1

mkdir -p "$W/home" "$W/project/.opencode" "$W/traces"
cat >"$W/project/opencode.json" <<JSON
{ "model": "mock/m", "small_model": "mock/m",
  "plugin": ["opencode-plugin-that-would-come-from-npm"],
  "provider": { "mock": { "npm": "@ai-sdk/openai-compatible", "name": "Mock",
    "options": { "baseURL": "http://127.0.0.1:$MOCK/v1", "apiKey": "x" },
    "models": { "m": { "name": "Mock", "tool_call": true, "limit": { "context": 100000, "output": 4000 } } } } } }
JSON
export XDG_CONFIG_HOME=$W/home/config XDG_DATA_HOME=$W/home/data XDG_CACHE_HOME=$W/home/cache XDG_STATE_HOME=$W/home/state
export HTTPS_PROXY=http://127.0.0.1:$PROXY HTTP_PROXY=http://127.0.0.1:$PROXY https_proxy=http://127.0.0.1:$PROXY http_proxy=http://127.0.0.1:$PROXY
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
# Hostile environment: everything a user might set to switch network features back on.
export OPENCODE_ENABLE_EXA=1 OPENCODE_EXPERIMENTAL=1 OPENCODE_ENABLE_PARALLEL=1 OPENCODE_DISABLE_MODELS_FETCH=0 \
  OPENCODE_MODELS_URL=https://models.example.invalid OPENCODE_ALWAYS_NOTIFY_UPDATE=1
cd "$W/project"

SCENARIO=
mark() { SCENARIO=$1; echo "{\"kind\":\"scenario\",\"target\":\"$1\"}" >>"$LOG"; }
traced() { strace -f -qq -e trace=connect -o "$W/traces/$SCENARIO.$RANDOM" "$@"; }
oc() { timeout 90 strace -f -qq -e trace=connect -o "$W/traces/$SCENARIO.$RANDOM" "$OC" "$@" </dev/null 2>&1; }

mark version;  oc --version >/dev/null
mark models;   oc models >/dev/null
mark chat;     chat=$(oc run "say hello")
mark tool;     tool=$(oc run "RUN: echo egress-test")
mark upgrade;  oc upgrade >/dev/null
mark console;  oc console login >/dev/null
mark github;   oc github install >/dev/null
mark serve
traced "$OC" serve --port "$SERVE" --hostname 127.0.0.1 >"$W/serve.log" 2>&1 &
serve_pid=$!
for _ in $(seq 60); do grep -q listening "$W/serve.log" 2>/dev/null && break; sleep 0.5; done
sleep 2
ui_status=$(curl -s -m 30 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$SERVE/")
curl -s -m 30 -o /dev/null "http://127.0.0.1:$SERVE/session?directory=$W/project"
sleep 3
kill "$serve_pid" 2>/dev/null; wait "$serve_pid" 2>/dev/null
tui=skipped
if command -v tmux >/dev/null; then
  mark tui
  tmux new-session -d -s fork-egress -x 160 -y 40 -c "$W/project" \
    "env TERM=xterm-256color strace -f -qq -e trace=connect -o '$W/traces/tui.0' '$OC'; sleep 120"
  sleep 25
  tmux send-keys -t fork-egress "say hello"; sleep 0.5; tmux send-keys -t fork-egress Enter
  sleep 15
  tui=$(tmux capture-pane -p -t fork-egress | grep -c 'Hello from the mock model')
  tmux kill-session -t fork-egress 2>/dev/null
fi
mark done

echo "functional: chat=$(grep -c 'Hello from the mock model' <<<"$chat") tool=$(grep -c 'Tool said: egress-test' <<<"$tool") tui=$tui web-ui=HTTP $ui_status"
echo "destinations attempted (destination  count  during):"
python3 - "$LOG" "$W/traces" "$PROXY" "$MOCK" "$SERVE" >"$W/summary" <<'PY'
import json, os, re, sys
log, traces, *local_ports = sys.argv[1:]
seen = {}
def add(target, where):
    seen.setdefault(target, []).append(where)

cur = "?"
for line in open(log):
    r = json.loads(line)
    if r["kind"] == "scenario":
        cur = r["target"]
    else:
        add(f"{r['target']} (via proxy)", cur)

inet = re.compile(r'connect\(\d+, \{sa_family=AF_INET6?, sin6?_port=htons\((\d+)\), (?:sin_addr=inet_addr\("([^"]+)"\)|.*?inet_pton\(AF_INET6, "([^"]+)")')
for name in sorted(os.listdir(traces)):
    scenario = name.split(".")[0]
    for line in open(os.path.join(traces, name), errors="replace"):
        m = inet.search(line)
        if not m:
            continue
        port, addr = m.group(1), m.group(2) or m.group(3)
        loopback = addr.startswith("127.") or addr in ("::1", "::ffff:127.0.0.1")
        if loopback and port in local_ports:
            continue  # the recording proxy, the mock model, the local server
        if port == "0":
            continue  # connect() with port 0 probes source addresses; nothing is sent
        add(f"{addr}:{port}" + (" (DNS)" if port == "53" else ""), scenario)

for target, where in sorted(seen.items()):
    print(f"  {target:44s} {len(where):3d}  {', '.join(sorted(set(where)))}")
if not seen:
    print("  (none)")
PY
cat "$W/summary"
if [ "$EXPECT_NONE" = "--expect-none" ]; then
  if grep -q '(none)' "$W/summary"; then echo "PASS: no outbound connection attempts"; else echo "FAIL: outbound connection attempts"; exit 1; fi
fi
