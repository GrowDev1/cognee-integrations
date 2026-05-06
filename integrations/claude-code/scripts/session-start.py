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
from config import (
    ensure_cognee_ready,
    ensure_identity,
    get_dataset,
    get_session_id,
    load_config,
    save_config,
)

_RESOLVED_CACHE = Path.home() / ".cognee-plugin" / "resolved.json"
_WATCHER_PID = Path.home() / ".cognee-plugin" / "watcher.pid"
_WATCHER_STOP = Path.home() / ".cognee-plugin" / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _spawn_idle_watcher(session_id: str, dataset: str, config: dict) -> None:
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
        except Exception:
            pass

    # Clear any stale stop sentinel from a previous run.
    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception:
        pass

    # Only the non-secret surface of config needs to travel — the
    # watcher re-runs ``ensure_cognee_ready`` on its own.
    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "config": {
            "service_url": config.get("service_url", ""),
            "api_key": config.get("api_key", ""),
            "llm_api_key": config.get("llm_api_key", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = Path.home() / ".cognee-plugin" / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception:
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


async def _start(out_stream=None):
    config = load_config()
    cwd = os.environ.get("CLAUDE_CWD", os.getcwd())

    session_id = get_session_id(config, cwd)
    dataset = get_dataset(config)

    # Configure cognee (cloud or local)
    try:
        await ensure_cognee_ready(config)
    except Exception as e:
        print(f"cognee-plugin: init warning ({e})", file=sys.stderr)

    # Register agent identity (claude-code@cognee.agent)
    user_id = ""
    agent_api_key = ""
    try:
        user_id, agent_api_key = await ensure_identity(config)
    except Exception as e:
        print(f"cognee-plugin: identity warning ({e})", file=sys.stderr)

    # Write resolved values for other hooks
    _write_resolved(session_id, dataset, user_id, cwd, api_key=agent_api_key)

    # Create config file on first run if it doesn't exist
    config_file = Path.home() / ".cognee-plugin" / "config.json"
    if not config_file.exists():
        save_config(config)

    # Launch the idle watcher. If COGNEE_IDLE_DISABLED is set, skip it.
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() not in ("1", "true", "yes"):
        _spawn_idle_watcher(session_id, dataset, config)

    mode = "cloud" if config.get("service_url") else "local"
    print(
        f"cognee-plugin: session ready (mode={mode}, "
        f"session={session_id}, dataset={dataset}, user={user_id[:8]}...)",
        file=sys.stderr,
    )

    # Inject system guidance so Claude knows how to route data
    guidance = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "systemMessage": (
                "## Cognee Memory Connected\n"
                f"Mode: {mode} | Dataset: {dataset} | Session: {session_id}\n\n"
                "Cognee organizes knowledge into three categories. "
                "When storing data with /cognee-memory:cognee-remember, "
                "route to the correct category:\n\n"
                "- **user_context** — user preferences, corrections, personal facts, "
                "communication style. Use when the user says 'remember my preference', "
                "'I always want', or shares personal details.\n"
                "- **project_docs** — repository docs, code context, architecture decisions, "
                "company data. Use when storing codebase knowledge, API docs, or project context.\n"
                "- **agent_actions** — reasoning traces, conclusions, discovered patterns. "
                "Use when you want to persist your own findings. "
                "Routine tool call logging is automatic (no action needed).\n\n"
                "When searching with /cognee-memory:cognee-search, you can filter by category "
                "using --node-set (user_context, project_docs, or agent_actions).\n"
                "If unsure which category, default to project_docs."
            ),
        }
    }
    print(json.dumps(guidance), file=out_stream or sys.stdout)


def main():
    # Read stdin (SessionStart payload) — consumed but not used
    sys.stdin.read()

    # Claude Code expects pure JSON on stdout for hookSpecificOutput. Some
    # cognee codepaths print human-facing banners directly to stdout, which
    # would contaminate the hook output and prevent systemMessage from
    # rendering in the user's terminal. Redirect stdout to stderr while we
    # run, then write our JSON to the saved real stdout at the very end.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        asyncio.run(_start(real_stdout))
    except Exception as exc:
        print(f"cognee-plugin: session start failed ({exc})", file=sys.stderr)
    finally:
        sys.stdout = real_stdout


if __name__ == "__main__":
    main()
