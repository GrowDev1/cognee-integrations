"""Shared helpers across plugin hook scripts.

Kept deliberately small: user resolution, resolved-cache read, a
single log-to-disk helper. Hook scripts shouldn't grow heavy because
they run on every user prompt / tool call.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PLUGIN_DIR = Path.home() / ".cognee-plugin"
_RESOLVED_CACHE = _PLUGIN_DIR / "resolved.json"
_HOOK_LOG = _PLUGIN_DIR / "hook.log"
_COUNTER_FILE = _PLUGIN_DIR / "counter.json"
_ACTIVITY_FILE = _PLUGIN_DIR / "activity.ts"
_ACTIVITY_LOG = _PLUGIN_DIR / "activity.log"
_SAVE_COUNTER = _PLUGIN_DIR / "save_counter.json"

# Save-kinds tracked per turn. Keep this tuple in sync with bump_save_counter callers.
SAVE_KINDS = ("prompt", "trace", "answer")

# Cap the per-line log size so a noisy tool output doesn't bloat the file.
_LOG_LINE_CAP = 600

# Default auto-improve threshold (tool calls + stops). Env override.
AUTO_IMPROVE_EVERY_DEFAULT = 30


def load_resolved() -> dict:
    """Load the SessionStart-cached session state."""
    if _RESOLVED_CACHE.exists():
        try:
            return json.loads(_RESOLVED_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


async def resolve_user(user_id: str):
    """Resolve cached user ID to a User object, or fall back to default."""
    if user_id:
        try:
            from uuid import UUID

            from cognee.modules.users.methods import get_user

            user = await get_user(UUID(user_id))
            if user:
                return user
        except Exception:
            pass
    from cognee.modules.users.methods import get_default_user

    return await get_default_user()


def hook_log(event: str, detail: Optional[dict] = None) -> None:
    """Append one structured line to ~/.cognee-plugin/hook.log.

    Safe to call silently — never raises. Use for forensic debugging
    of why a hook did (or did not) write something to memory.
    """
    try:
        _HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "event": event,
        }
        if detail:
            line["detail"] = detail
        serialized = json.dumps(line, default=str)
        if len(serialized) > _LOG_LINE_CAP:
            serialized = serialized[: _LOG_LINE_CAP - 3] + "..."
        with _HOOK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(serialized + "\n")
    except Exception:
        pass


def _verbose_enabled() -> bool:
    return os.environ.get("COGNEE_PLUGIN_VERBOSE", "").lower() in ("1", "true", "yes")


def notify(msg: str) -> None:
    """Print a status line to stderr (shown under the hook's status indicator).

    When ``COGNEE_PLUGIN_VERBOSE=1`` is set, also append a timestamped
    line to ``~/.cognee-plugin/activity.log`` so saves that happen in
    async hooks are ``tail -f``-visible (they never surface in the
    Claude transcript on their own).
    """
    line = f"cognee-plugin: {msg}"
    print(line, file=sys.stderr)
    if _verbose_enabled():
        try:
            _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with _ACTIVITY_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{ts} {line}\n")
        except Exception:
            pass


def bump_save_counter(session_id: str, kind: str) -> None:
    """Record a save of ``kind`` (one of ``SAVE_KINDS``) for this session.

    Used to surface per-turn save volume back to the user through the
    next UserPromptSubmit's injected context. Cheap, best-effort file IO —
    never raises.
    """
    if not session_id or kind not in SAVE_KINDS:
        return
    try:
        data = (
            json.loads(_SAVE_COUNTER.read_text(encoding="utf-8")) if _SAVE_COUNTER.exists() else {}
        )
    except Exception:
        data = {}
    sess = data.get(session_id) or {k: 0 for k in SAVE_KINDS}
    sess[kind] = int(sess.get(kind, 0)) + 1
    data[session_id] = sess
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _SAVE_COUNTER.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def read_and_reset_save_counter(session_id: str) -> dict:
    """Return the save-kind counts accumulated since the last reset, then zero them."""
    zero = {k: 0 for k in SAVE_KINDS}
    if not session_id:
        return zero
    try:
        data = (
            json.loads(_SAVE_COUNTER.read_text(encoding="utf-8")) if _SAVE_COUNTER.exists() else {}
        )
    except Exception:
        return zero
    sess = data.get(session_id) or zero
    data[session_id] = dict(zero)
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _SAVE_COUNTER.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    return {k: int(sess.get(k, 0)) for k in SAVE_KINDS}


def _auto_improve_threshold() -> int:
    raw = os.environ.get("COGNEE_AUTO_IMPROVE_EVERY", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return AUTO_IMPROVE_EVERY_DEFAULT


def bump_turn_counter(session_id: str) -> tuple[int, bool]:
    """Increment the per-session tool-call counter.

    Returns (new_count, should_improve). ``should_improve`` is True when
    the count crossed a multiple of the configured threshold — the
    caller is expected to fire ``improve()`` and proceed.

    Counter survives across hook invocations via a tiny JSON file.
    Concurrent writes: we accept rare off-by-one drift under heavy
    parallel tool use — this is a heartbeat, not a ledger.
    """
    if not session_id:
        return 0, False

    threshold = _auto_improve_threshold()

    data: dict = {}
    if _COUNTER_FILE.exists():
        try:
            data = json.loads(_COUNTER_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    count = int(data.get(session_id, 0)) + 1
    data[session_id] = count

    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _COUNTER_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass

    should_improve = threshold > 0 and count % threshold == 0
    return count, should_improve


def touch_activity() -> None:
    """Update the last-activity timestamp for the idle watcher."""
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _ACTIVITY_FILE.write_text(str(datetime.now(timezone.utc).timestamp()), encoding="utf-8")
    except Exception:
        pass


def _local_api_url() -> str:
    return os.environ.get("COGNEE_LOCAL_API_URL", "http://localhost:8000")


def _backend_reachable(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/docs", timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def improve_via_http(
    dataset: str,
    session_id: str,
    run_in_background: bool = False,
    timeout: float = 600.0,
) -> bool:
    """POST /api/v1/improve to the running backend if reachable.

    Returns True on HTTP 2xx, False if the backend is unreachable or the
    request failed. Callers should fall back to the local SDK path on False.

    Purpose: Kuzu is a single-writer graph DB. When the backend server is
    running it holds the lock; a second process importing ``cognee.improve()``
    fails mid-pipeline. Routing through HTTP lets the server — which owns
    the lock — run improve() in its own process.
    """
    base_url = _local_api_url()
    if not _backend_reachable(base_url):
        return False
    url = f"{base_url.rstrip('/')}/api/v1/improve"
    payload = json.dumps(
        {
            "dataset_name": dataset,
            "session_ids": [session_id],
            "run_in_background": run_in_background,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            hook_log(
                "improve_http_ok",
                {"status": resp.status, "dataset": dataset, "session": session_id},
            )
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        hook_log(
            "improve_http_failed",
            {"error": str(exc)[:200], "dataset": dataset, "session": session_id},
        )
        return False
