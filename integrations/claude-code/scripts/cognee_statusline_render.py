#!/usr/bin/env python3
"""Render the Cognee status line.

Invoked by Claude Code's ``statusLine`` (via ``cognee-statusline.sh``), which
pipes a JSON context on stdin that includes ``session_id`` (the host session id).

Deliberately standalone and pure-local: it reads only small JSON files under
``~/.cognee-plugin`` and never imports the plugin's ``_plugin_common`` (whose
import re-execs into the cognee venv) or makes any network call, so it stays
instant and runs safely on every status refresh.

Output: ``cognee: <cognee-session-id> (+N more)``.
"""

import json
import sys
from pathlib import Path

_PLUGIN_DIR = Path.home() / ".cognee-plugin"
_SESSIONS_DIR = _PLUGIN_DIR / "sessions"
_COUNT_CACHE = _PLUGIN_DIR / "sessions_count.json"


def _sanitize(value: str) -> str:
    safe = []
    for ch in str(value or ""):
        safe.append(ch if (ch.isalnum() or ch in ("-", "_", ".")) else "_")
    return "".join(safe).strip("._")[:120]


def _current_session(host_id: str) -> str:
    if not host_id:
        return ""
    path = _SESSIONS_DIR / f"{_sanitize(host_id)}.json"
    try:
        if path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rec, dict):
                return str(rec.get("session_id") or "").strip()
    except Exception:
        pass
    return ""


def _total_sessions() -> int | None:
    try:
        if _COUNT_CACHE.exists():
            data = json.loads(_COUNT_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "total" in data:
                return int(data["total"])
    except Exception:
        pass
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    host_id = str(payload.get("session_id") or "").strip() if isinstance(payload, dict) else ""

    session_id = _current_session(host_id)
    if not session_id:
        # No mapped session yet (server still warming / not registered).
        sys.stdout.write("cognee: starting...")
        return

    total = _total_sessions()
    extra = ""
    if total is not None:
        others = total - 1 if total > 0 else 0
        if others > 0:
            extra = f" (+{others} more)"
    sys.stdout.write(f"cognee: {session_id}{extra}")


if __name__ == "__main__":
    main()
