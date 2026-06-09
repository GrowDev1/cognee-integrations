#!/usr/bin/env bash
# Cognee status line for Codex.
# Reads small JSON state files written by the cognee-memory plugin
# (~/.cognee-plugin/codex/) and renders a one-line summary at the bottom
# of the terminal. Best-effort: any missing piece is silently omitted.

set -u

PLUGIN_DIR="${HOME}/.cognee-plugin/codex"
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

runtime_json="$(python3 - <<'PY' "${PLUGIN_DIR}" 2>/dev/null || true
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

plugin_dir = pathlib.Path(sys.argv[1])
service_url = (os.environ.get("COGNEE_SERVICE_URL") or os.environ.get("COGNEE_LOCAL_API_URL") or "http://localhost:8011").strip()
api_key = (os.environ.get("COGNEE_API_KEY") or "").strip()
agent_name = (os.environ.get("COGNEE_AGENT_NAME") or "").strip()
if agent_name:
    if agent_name.endswith("@cognee.agent"):
        agent_name = agent_name[: -len("@cognee.agent")]
    if not agent_name.endswith("_codex"):
        agent_name = f"{agent_name}_codex"

if not api_key and service_url and agent_name:
    cache_path = plugin_dir / "agent_keys.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
            if isinstance(entries, dict):
                normalized_url = service_url.rstrip("/")
                key = f"{normalized_url}::{agent_name}"
                chosen = entries.get(key)
                if isinstance(chosen, dict):
                    api_key = str(chosen.get("api_key") or "").strip()
                else:
                    for entry in entries.values():
                        if not isinstance(entry, dict):
                            continue
                        name = str(entry.get("agent_name") or "").strip()
                        url = str(entry.get("service_url") or "").strip().rstrip("/")
                        if name == agent_name and url == normalized_url:
                            api_key = str(entry.get("api_key") or "").strip()
                            break
        except Exception:
            pass

session_id = ""
dataset = ""
if service_url and api_key:
    try:
        query = ""
        session_key = (os.environ.get("COGNEE_SESSION_KEY") or "").strip()
        if session_key:
            query = "?agent_session_name=" + urllib.parse.quote(session_key, safe="")
        req = urllib.request.Request(
            service_url.rstrip("/") + "/api/v1/agents/connections/me" + query,
            headers={"X-Api-Key": api_key},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(payload, dict):
            agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
            if isinstance(agent, dict):
                session_id = str(agent.get("session_id") or "").strip()
                datasets = agent.get("datasets") if isinstance(agent.get("datasets"), list) else []
                for item in datasets:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if name:
                            dataset = name
                            break
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        pass

print(json.dumps({"session_id": session_id, "dataset": dataset, "api_key_present": bool(api_key)}))
PY
)"

session_id="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("session_id") or "").strip())
except Exception:
    pass
PY
)"
dataset="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("dataset") or "").strip())
except Exception:
    pass
PY
)"
api_key_present="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print("1" if json.loads(sys.argv[1] or "{}").get("api_key_present") else "")
except Exception:
    pass
PY
)"

# Static (option 1)
mode="local"
[ -n "${api_key_present:-}" ] && mode="cloud"
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
    print(f"recall: {s}s/{t}t/{g}g")
except Exception:
    pass
PY
)
fi

# Warming indicator — an absent server-ready marker means the local Cognee
# server is still booting/migrating; recall is paused until it comes up.
warming=""
if [ ! -e "${HOME}/.cognee-plugin/server-ready.json" ]; then
    warming="cognee: warming"
fi

# Compose. Drop empty segments cleanly.
parts=()
parts+=("$static")
[ -n "$warming" ] && parts+=("$warming")
[ -n "$recall" ] && parts+=("$recall")
[ -n "$saves" ] && parts+=("$saves")

# Join with " | "
out=""
for p in "${parts[@]}"; do
    [ -z "$out" ] && out="$p" || out="${out} | ${p}"
done

printf '%s' "$out"
