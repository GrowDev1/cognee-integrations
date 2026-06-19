#!/usr/bin/env python3
"""List the principal's Cognee sessions for the session picker.

Prints JSON: ``{current, total, sessions: [{session_id, last_activity_at, status,
last_model}]}`` (most-recent first). The picker skill renders this as text and
lets the user pick (by id) which session this terminal should use.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (  # noqa: E402
    find_host_pid,
    list_sessions_via_http,
    read_host_key_for_pid,
    resolve_cognee_session_id,
)


def _current_session() -> str:
    host_key = read_host_key_for_pid(find_host_pid())
    return resolve_cognee_session_id(host_key) if host_key else ""


def main() -> None:
    current = _current_session()
    try:
        data = list_sessions_via_http(limit=200, range_="all")
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:200], "current": current, "sessions": []}))
        return

    raw = data.get("sessions", []) if isinstance(data, dict) else []
    rows = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        rows.append(
            {
                "session_id": str(s.get("session_id") or s.get("id") or ""),
                "last_activity_at": str(s.get("last_activity_at") or s.get("started_at") or ""),
                "status": str(s.get("status") or ""),
                "last_model": str(s.get("last_model") or ""),
            }
        )
    print(
        json.dumps(
            {
                "current": current,
                "total": data.get("total", len(rows)) if isinstance(data, dict) else len(rows),
                "sessions": rows,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
