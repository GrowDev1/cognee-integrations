#!/usr/bin/env python3
"""Bridge session cache entries into the permanent knowledge graph on session end.

Runs the integration's explicit session bridge:
  1. Persist session Q&A/trace cache into the permanent graph
  2. Sync graph knowledge back into the session cache for recall

Configuration:
    Uses resolved session ID and dataset from SessionStart hook
    (via ~/.cognee-plugin/codex/resolved.json). Falls back to env vars.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Add scripts dir to path for config/_plugin_common imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    hook_log,
    persist_session_cache_to_graph_via_http,
    resolve_user,
    sync_lock,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    is_cloud_mode,
    load_config,
    persist_session_cache_to_graph,
    sync_graph_context_to_session,
)

_STATE_DIR = Path.home() / ".cognee-plugin" / "codex"
_RESOLVED_CACHE = _STATE_DIR / "resolved.json"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_DETACHED_ARG = "--detached-final"
_SESSION_END_ARG = "--session-end"
_DETACHED_RETRIES_DEFAULT = 3
_DETACHED_RETRY_DELAY_DEFAULT = 10.0
_SESSION_END_START_DELAY_DEFAULT = 2.0


def _stop_idle_watcher() -> None:
    """Signal the idle watcher to exit and drop its pidfile.

    Uses both a sentinel file (safe, polled by the watcher) and a
    SIGTERM (fast). Either path is sufficient; both together handle
    the SIGTERM-blocked-during-improve edge case.
    """
    try:
        _WATCHER_STOP.parent.mkdir(parents=True, exist_ok=True)
        _WATCHER_STOP.write_text("stop", encoding="utf-8")
    except Exception as exc:
        hook_log("watcher_stop_write_failed", {"error": str(exc)[:200]})
    if _WATCHER_PID.exists():
        try:
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            hook_log("watcher_sigterm_failed", {"error": str(exc)[:200]})


def _spawn_detached_sync() -> bool:
    """Run the expensive sync outside a short hook window."""
    try:
        env = os.environ.copy()
        env.setdefault("COGNEE_SYNC_START_DELAY", str(_SESSION_END_START_DELAY_DEFAULT))
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), _DETACHED_ARG],
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:
        hook_log("sync_detach_failed", {"error": str(exc)[:300]})
        return False


def _is_session_end_payload(payload_raw: str) -> bool:
    """Return True only for an actual SessionEnd hook payload."""
    if not payload_raw.strip():
        return False
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return False

    def _contains_session_end(value) -> bool:
        if isinstance(value, dict):
            return any(_contains_session_end(item) for item in value.values())
        if isinstance(value, list):
            return any(_contains_session_end(item) for item in value)
        if isinstance(value, str):
            return value == "SessionEnd" or value.endswith(".SessionEnd")
        return False

    event = (
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event")
        or payload.get("hook")
    )
    return event == "SessionEnd" or _contains_session_end(payload)


def _load_resolved() -> tuple:
    """Load session ID, dataset, and user ID from resolved cache."""
    if _RESOLVED_CACHE.exists():
        try:
            data = json.loads(_RESOLVED_CACHE.read_text(encoding="utf-8"))
            return data.get("session_id", ""), data.get("dataset", ""), data.get("user_id", "")
        except Exception as exc:
            hook_log("sync_resolved_load_failed", {"error": str(exc)[:200]})
    config = load_config()
    return get_session_id(config), get_dataset(config), ""


async def _sync(stop_watcher: bool):
    session_id, dataset, user_id = _load_resolved()
    hook_log(
        "sync_start",
        {
            "session": session_id,
            "dataset": dataset,
            "user_id": user_id,
            "stop_watcher": stop_watcher,
        },
    )

    with sync_lock("sync-session-to-graph") as acquired:
        if not acquired:
            hook_log("sync_skipped_lock_busy", {"session": session_id, "dataset": dataset})
            print("cognee-sync: skipped, another sync is running", file=sys.stderr)
            return

        if stop_watcher:
            _stop_idle_watcher()
            hook_log("sync_stopped_watcher", {"session": session_id, "dataset": dataset})

        config = load_config()
        if is_cloud_mode(config):
            wrote = persist_session_cache_to_graph_via_http(dataset, session_id)
            hook_log(
                "sync_bridge_done",
                {"session": session_id, "dataset": dataset, "via": "http_remember", "wrote": wrote},
            )
            print(
                "cognee-sync: "
                f"dataset={dataset} session={session_id} via=http_remember wrote={wrote}",
                file=sys.stderr,
            )
            return

        await ensure_cognee_ready(config)
        user = await resolve_user(user_id)
        await ensure_dataset_ready(dataset, user)
        wrote = await persist_session_cache_to_graph(dataset, session_id, user)
        graph_result = await sync_graph_context_to_session(dataset, session_id, user)

        hook_log(
            "sync_bridge_done",
            {
                "session": session_id,
                "dataset": dataset,
                "user_id": str(getattr(user, "id", "")),
                "wrote": wrote,
                "graph_synced": graph_result.get("synced", 0),
            },
        )
        print(
            "cognee-sync: "
            f"dataset={dataset} session={session_id} wrote={wrote} "
            f"graph_synced={graph_result.get('synced', 0)}",
            file=sys.stderr,
        )


def main():
    detached_final = _DETACHED_ARG in sys.argv
    forced_session_end = _SESSION_END_ARG in sys.argv
    payload_raw = "" if detached_final else sys.stdin.read()
    is_session_end = forced_session_end or _is_session_end_payload(payload_raw)
    hook_log(
        "sync_payload",
        {
            "is_session_end": is_session_end,
            "detached_final": detached_final,
            "forced_session_end": forced_session_end,
            "payload_preview": payload_raw[:200],
        },
    )

    if detached_final:
        delay_raw = os.environ.get("COGNEE_SYNC_START_DELAY", "")
        try:
            delay = float(delay_raw) if delay_raw else 0.0
        except ValueError:
            delay = 0.0
        if delay > 0:
            hook_log("sync_start_delayed", {"seconds": delay})
            time.sleep(delay)

    # Only a true SessionEnd should stop the watcher. Manual syncs and
    # slash-command invocations happen mid-session, and killing the watcher
    # there prevents later idle persistence.
    if is_session_end:
        _stop_idle_watcher()
        spawned = _spawn_detached_sync()
        hook_log("sync_deferred_to_shutdown_worker", {"spawned": spawned})
        return

    attempts = 1
    retry_delay = 0.0
    if detached_final:
        attempts = int(os.environ.get("COGNEE_SYNC_RETRIES", str(_DETACHED_RETRIES_DEFAULT)))
        retry_delay = float(
            os.environ.get("COGNEE_SYNC_RETRY_DELAY", str(_DETACHED_RETRY_DELAY_DEFAULT))
        )

    for attempt in range(1, max(1, attempts) + 1):
        try:
            asyncio.run(_sync(stop_watcher=False))
            return
        except Exception as exc:
            # Non-fatal: session sync failure should not crash Codex.
            hook_log(
                "sync_failed",
                {"attempt": attempt, "attempts": attempts, "error": str(exc)[:300]},
            )
            print(f"cognee-sync: failed ({exc})", file=sys.stderr)
            if attempt < attempts:
                hook_log("sync_retry_scheduled", {"attempt": attempt + 1, "delay": retry_delay})
                time.sleep(retry_delay)


if __name__ == "__main__":
    main()
