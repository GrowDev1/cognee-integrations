#!/usr/bin/env bash
# Cognee status line for Claude Code.
# Reads small JSON state files written by the cognee-memory plugin
# (~/.cognee-plugin/) and renders a one-line summary at the bottom
# of the terminal. Best-effort: any missing piece is silently omitted.

set -u

PLUGIN_DIR="${HOME}/.cognee-plugin"
RESOLVED="${PLUGIN_DIR}/resolved.json"
LAST_RECALL="${PLUGIN_DIR}/last_recall.json"
SAVE_COUNTER="${PLUGIN_DIR}/save_counter.json"

# Pull values via python (always present in cognee dev envs); fall back to
# raw cat if python is unavailable so the script never errors out hard.
read_json() {
    local file="$1" key="$2"
    [ -r "$file" ] || return 0
    python3 - "$file" "$key" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    parts = sys.argv[2].split(".")
    cur = data
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            sys.exit(0)
    if isinstance(cur, (dict, list)):
        print(json.dumps(cur))
    else:
        print(cur)
except Exception:
    pass
PY
}

session_id=$(read_json "$RESOLVED" session_id)
dataset=$(read_json "$RESOLVED" dataset)
api_key=$(read_json "$RESOLVED" api_key)

# Static (option 1)
mode="local"
[ -n "${api_key:-}" ] && mode="cloud"
sess_short="${session_id##*_}"   # last hash segment, e.g. 74f2b7ad530a
[ -z "$sess_short" ] && sess_short="-"
[ -z "${dataset:-}" ] && dataset="-"

static="cognee[$mode] ds=${dataset} sess=${sess_short}"

# Save counts (option 2) — read current snapshot of the per-session counter.
# This file is reset by each UserPromptSubmit hook, so what's here is a live
# running tally for the *current* turn (saves accumulate during the turn).
saves=""
if [ -r "$SAVE_COUNTER" ] && [ -n "${session_id:-}" ]; then
    saves=$(python3 - "$SAVE_COUNTER" "$session_id" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    sess = data.get(sys.argv[2]) or {}
    p = int(sess.get("prompt", 0))
    t = int(sess.get("trace", 0))
    a = int(sess.get("answer", 0))
    if p or t or a:
        print(f"saving: {p}p/{t}t/{a}a")
except Exception:
    pass
PY
)
fi

# Last recall (option 3) — counts from the most recent UserPromptSubmit hook.
recall=""
if [ -r "$LAST_RECALL" ]; then
    recall=$(python3 - "$LAST_RECALL" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    h = data.get("hits") or {}
    s = int(h.get("session", 0))
    t = int(h.get("trace", 0))
    g = int(h.get("graph_context", 0))
    total = s + t + g
    icon = "🔍" if total else "·"
    print(f"{icon} recall: {s}s/{t}t/{g}g")
except Exception:
    pass
PY
)
fi

# Compose. Drop empty segments cleanly.
parts=()
parts+=("$static")
[ -n "$recall" ] && parts+=("$recall")
[ -n "$saves" ] && parts+=("$saves")

# Join with " | "
out=""
for p in "${parts[@]}"; do
    [ -z "$out" ] && out="$p" || out="${out} | ${p}"
done

printf '%s' "$out"
