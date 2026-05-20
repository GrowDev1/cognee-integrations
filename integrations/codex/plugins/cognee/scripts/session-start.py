#!/usr/bin/env python3
"""Initialize Cognee memory at session start.

Runs on the SessionStart hook. Responsibilities:
  1. Load config (file + env vars)
  2. Compute per-directory session ID
  3. Connect to Cognee Cloud if configured
  4. Configure local LLM if local mode
  5. Write resolved session ID to env cache for other hooks

The resolved session ID and dataset are written to a cache file
so that the other hook scripts (which run in separate processes)
can pick them up without re-computing.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log, quiet_hook_output, touch_activity
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    ensure_dataset_ready_via_api,
    ensure_identity,
    get_dataset,
    get_session_id,
    is_cloud_mode,
    load_config,
    save_config,
)

_STATE_DIR = Path.home() / ".cognee-plugin" / "codex"
_RESOLVED_CACHE = _STATE_DIR / "resolved.json"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")
_EXIT_WATCHER_PID = _STATE_DIR / "exit-watcher.pid"
_EXIT_WATCHER_SCRIPT = Path(__file__).with_name("exit-watcher.py")


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _spawn_idle_watcher(session_id: str, dataset: str, user_id: str, config: dict) -> None:
    """Launch the idle watcher as a detached background process.

    Idempotent: if a watcher is already alive (from an earlier session
    on the same machine), we kill it so the new one picks up the new
    session. Launched with its own session via ``start_new_session=True``
    so it survives the parent shell closing.
    """
    if _watcher_alive():
        try:
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            hook_log("idle_watcher_kill_failed", {"error": str(exc)[:200]})

    # Clear any stale stop sentinel from a previous run.
    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception as exc:
        hook_log("watcher_stop_unlink_failed", {"error": str(exc)[:200]})

    # Only the non-secret surface of config needs to travel — the
    # watcher re-runs ``ensure_cognee_ready`` on its own.
    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "config": {
            "service_url": config.get("service_url", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = _STATE_DIR / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
        print("cognee-plugin: idle watcher started", file=sys.stderr)
    except Exception as e:
        print(f"cognee-plugin: idle watcher launch failed ({e})", file=sys.stderr)


def _find_codex_parent_pid() -> int:
    """Find the nearest live Codex ancestor, skipping hook shells."""
    fallback = os.getppid()
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        hook_log("find_codex_parent_failed", {"error": str(exc)[:200]})
        return fallback

    table: dict[int, tuple[int, str]] = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2])

    pid = fallback
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        ppid, command = table.get(pid, (0, ""))
        executable = Path(command.split()[0]).name if command else ""
        if executable == "codex" or executable.startswith("codex-"):
            return pid
        pid = ppid
    return fallback


def _spawn_exit_watcher(session_id: str, dataset: str) -> None:
    """Launch a detached watcher that syncs only after Codex exits."""
    try:
        if _EXIT_WATCHER_PID.exists():
            pid = int(_EXIT_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
    except Exception as exc:
        hook_log("exit_watcher_kill_failed", {"error": str(exc)[:200]})

    bootstrap = {
        "parent_pid": _find_codex_parent_pid(),
        "session_id": session_id,
        "dataset": dataset,
    }
    log_path = _STATE_DIR / "exit-watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("exit_watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        subprocess.Popen(
            [sys.executable, str(_EXIT_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
        hook_log("exit_watcher_started", bootstrap)
    except Exception as e:
        hook_log("exit_watcher_launch_failed", {"error": str(e)[:300]})


def _write_resolved(
    session_id: str, dataset: str, user_id: str, cwd: str, api_key: str = ""
) -> None:
    """Cache resolved session ID, dataset, user ID, and API key for other hook scripts."""
    _RESOLVED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "cwd": cwd,
    }
    if api_key:
        data["api_key"] = api_key
    _RESOLVED_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _start(payload: dict | None = None) -> dict:
    config = load_config()
    payload = payload or {}
    cwd = str(payload.get("cwd") or os.environ.get("CODEX_CWD") or os.getcwd())

    session_id = get_session_id(config, cwd)
    dataset = get_dataset(config)

    # Configure cognee (cloud or local)
    try:
        await ensure_cognee_ready(config)
    except Exception as e:
        print(f"cognee-plugin: init warning ({e})", file=sys.stderr)

    # Register agent identity (codex@cognee.agent)
    user_id = ""
    agent_api_key = ""
    try:
        user_id, agent_api_key = await ensure_identity(config)
    except Exception as e:
        print(f"cognee-plugin: identity warning ({e})", file=sys.stderr)

    try:
        if user_id and is_cloud_mode(config):
            await ensure_dataset_ready_via_api(
                config.get("service_url", ""),
                agent_api_key or config.get("api_key", ""),
                dataset,
            )
        elif user_id:
            from uuid import UUID

            from cognee.modules.users.methods import get_user

            user = await get_user(UUID(user_id))
            await ensure_dataset_ready(dataset, user)
    except Exception as e:
        print(f"cognee-plugin: dataset warning ({e})", file=sys.stderr)

    # Write resolved values for other hooks
    _write_resolved(session_id, dataset, user_id, cwd, api_key=agent_api_key)

    # Create config file on first run if it doesn't exist
    config_file = Path.home() / ".cognee-plugin" / "config.json"
    if not config_file.exists():
        save_config(config)

    # Reset the idle clock for this Codex process before the watcher
    # starts, otherwise a stale timestamp from a prior session can cause
    # an immediate improve on startup.
    touch_activity()

    # Launch the idle watcher. If COGNEE_IDLE_DISABLED is set, skip it.
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() not in ("1", "true", "yes"):
        _spawn_idle_watcher(session_id, dataset, user_id, config)

    _spawn_exit_watcher(session_id, dataset)

    mode = "cloud" if config.get("service_url") else "local"
    print(
        f"cognee-plugin: session ready (mode={mode}, "
        f"session={session_id}, dataset={dataset}, user={user_id[:8]}...)",
        file=sys.stderr,
    )

    return {}


def main():
    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    # Keep SessionStart output as a valid empty hook result. Recall context is
    # injected on UserPromptSubmit, matching the original Codex hook contract.
    try:
        with quiet_hook_output("session-start"):
            asyncio.run(_start(payload))
    except Exception as exc:
        hook_log("session_start_exception", {"error": str(exc)[:200]})
    print("{}")


if __name__ == "__main__":
    main()
