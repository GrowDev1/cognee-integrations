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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
_EXIT_WATCHERS_DIR = _STATE_DIR / "exit-watchers"
_AGENT_KEYS_CACHE = _STATE_DIR / "agent_keys.json"
_LOCAL_SERVICE_URL = "http://localhost:8011"
_HEALTH_URL = f"{_LOCAL_SERVICE_URL}/health"
_HEALTH_TIMEOUT_SECONDS = 30
_HEALTH_POLL_SECONDS = 1.0


def _health_ok(url: str = _HEALTH_URL, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _ensure_local_server_running(config: dict) -> None:
    if _health_ok():
        config["service_url"] = _LOCAL_SERVICE_URL
        os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL
        return

    server_env = os.environ.copy()
    server_env.setdefault("COGNEE_AGENT_MODE", "true")
    subprocess.Popen(
        ["uvicorn", "cognee.api.client:app", "--port", "8011"],
        env=server_env,
        start_new_session=True,
    )

    deadline = time.monotonic() + _HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _health_ok():
            config["service_url"] = _LOCAL_SERVICE_URL
            os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL
            return
        time.sleep(_HEALTH_POLL_SECONDS)

    raise RuntimeError(
        f"Cognee server did not become healthy at {_HEALTH_URL} within {_HEALTH_TIMEOUT_SECONDS}s"
    )


def _load_agent_keys_cache() -> dict:
    empty = {"version": 1, "entries": {}}
    try:
        if _AGENT_KEYS_CACHE.exists():
            data = json.loads(_AGENT_KEYS_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
    except Exception as exc:
        hook_log("agent_keys_cache_load_failed", {"error": str(exc)[:200]})
    return empty


def _save_agent_keys_cache(data: dict) -> None:
    try:
        _AGENT_KEYS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _AGENT_KEYS_CACHE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        hook_log("agent_keys_cache_save_failed", {"error": str(exc)[:200]})


def _normalize_service_url(service_url: str) -> str:
    return str(service_url or "").strip().rstrip("/")


def _agent_cache_key(service_url: str, agent_name: str) -> str:
    return f"{_normalize_service_url(service_url)}::{agent_name}"


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _login_default_user_for_owner_api_key(service_url: str) -> str:
    import aiohttp

    base = _normalize_service_url(service_url)
    email = os.environ.get("DEFAULT_USER_EMAIL", "default_user@example.com")
    password = os.environ.get("DEFAULT_USER_PASSWORD", "default_password")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base}/api/v1/auth/login",
            data={"username": email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    "default-user login failed "
                    f"({resp.status}: {body[:200]}). "
                    "Set DEFAULT_USER_EMAIL/DEFAULT_USER_PASSWORD correctly."
                )
            login_data = await resp.json()
            jwt = str(login_data.get("access_token", "") or "")

        if not jwt:
            raise RuntimeError("default-user login returned no access token")

        async with session.get(
            f"{base}/api/v1/auth/api-keys",
            cookies={"auth_token": jwt},
        ) as resp:
            if resp.status == 200:
                keys = await resp.json()
                if isinstance(keys, list) and keys:
                    key = str(keys[0].get("key", "") or "")
                    if key:
                        return key

        async with session.post(
            f"{base}/api/v1/auth/api-keys",
            json={"name": "codex-owner-bootstrap"},
            cookies={"auth_token": jwt},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"default-user API key creation failed ({resp.status}: {body[:200]})"
                )
            payload = await resp.json()
            key = str(payload.get("key", "") or "")
            if not key:
                raise RuntimeError("default-user API key creation returned empty key")
            return key


def _resolve_agent_name(config: dict, cwd: str) -> str:
    configured = str(config.get("agent_name", "") or "").strip()
    if configured:
        return configured
    return f"codex-{Path(cwd).name}"


async def _create_agent_with_bootstrap_key(
    service_url: str,
    agent_name: str,
    bootstrap_key: str,
) -> tuple[str, str]:
    import aiohttp

    headers = {"Content-Type": "application/json"}
    if bootstrap_key:
        headers["X-Api-Key"] = bootstrap_key

    base = service_url.rstrip("/")
    async with aiohttp.ClientSession(headers=headers) as session:
        # Use the canonical trailing-slash route to avoid a 307 redirect
        # that can drop auth headers in some clients/environments.
        async with session.post(f"{base}/api/v1/agents/", params={"name": agent_name}) as resp:
            if resp.status == 200:
                payload = await resp.json()
                return str(payload.get("agentId", "") or ""), str(
                    payload.get("agentApiKey", "") or ""
                )
            if resp.status == 409:
                raise RuntimeError(
                    f"Agent '{agent_name}' already exists on {base}, "
                    "but no cached API key was found. Delete and recreate the agent."
                )
            text = await resp.text()
            raise RuntimeError(f"create_agent failed ({resp.status}: {text[:200]})")


async def _ensure_agent_credentials_and_register(
    config: dict, cwd: str, session_id: str
) -> tuple[str, str, str, bool]:
    service_url = _normalize_service_url(str(config.get("service_url", "") or ""))
    if not service_url:
        return "", "", "", False

    agent_name = _resolve_agent_name(config, cwd)
    cache = _load_agent_keys_cache()
    entries = cache.get("entries", {})
    cache_key = _agent_cache_key(service_url, agent_name)
    cached = entries.get(cache_key, {}) if isinstance(entries, dict) else {}
    agent_id = str(cached.get("agent_id", "") or "")
    agent_api_key = str(cached.get("api_key", "") or "")

    try:
        previous = (
            json.loads(_RESOLVED_CACHE.read_text(encoding="utf-8"))
            if _RESOLVED_CACHE.exists()
            else {}
        )
    except Exception:
        previous = {}
    if (
        previous.get("session_id") == session_id
        and _normalize_service_url(previous.get("service_url", "")) == service_url
        and previous.get("agent_name") == agent_name
        and bool(previous.get("registered", False))
        and previous.get("api_key")
    ):
        existing_key = str(previous.get("api_key") or "")
        os.environ["COGNEE_API_KEY"] = existing_key
        config["api_key"] = existing_key
        return (
            str(previous.get("agent_id", "") or agent_id),
            existing_key,
            agent_name,
            True,
        )

    if not agent_api_key:
        bootstrap_key = str(
            config.get("api_key", "") or os.environ.get("COGNEE_API_KEY", "")
        ).strip()
        if not bootstrap_key:
            bootstrap_key = await _login_default_user_for_owner_api_key(service_url)
        created_agent_id, created_key = await _create_agent_with_bootstrap_key(
            service_url, agent_name, bootstrap_key
        )
        if created_key:
            agent_id = created_agent_id
            agent_api_key = created_key
            entries[cache_key] = {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "api_key": agent_api_key,
                "service_url": service_url,
                "created_at": _utc_iso_now(),
                "last_used_at": _utc_iso_now(),
            }
            cache["entries"] = entries
            _save_agent_keys_cache(cache)
    else:
        cached["last_used_at"] = _utc_iso_now()
        entries[cache_key] = cached
        cache["entries"] = entries
        _save_agent_keys_cache(cache)

    if not agent_api_key:
        return "", "", agent_name, False

    os.environ["COGNEE_API_KEY"] = agent_api_key
    config["api_key"] = agent_api_key

    from _plugin_common import register_agent_via_http

    registered, active = register_agent_via_http()
    if not registered:
        raise RuntimeError(
            f"Failed to register agent '{agent_name}' on {service_url}. "
            "Cached key may be invalid. Delete and recreate the agent."
        )
    hook_log(
        "agent_register_result",
        {
            "agent_name": agent_name,
            "agent_id": agent_id,
            "registered": registered,
            "active_agents": active,
            "session_id": session_id,
        },
    )

    return agent_id, agent_api_key, agent_name, registered


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

    def _pid_alive(pid: int) -> bool:
        if pid <= 1:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    # Cleanup stale watcher pidfiles so the directory does not grow forever.
    try:
        if _EXIT_WATCHERS_DIR.exists():
            for pidfile in _EXIT_WATCHERS_DIR.glob("*.pid"):
                try:
                    pid = int(pidfile.read_text(encoding="utf-8").strip())
                    if not _pid_alive(pid):
                        pidfile.unlink()
                except Exception:
                    continue
    except Exception as exc:
        hook_log("exit_watcher_prune_failed", {"error": str(exc)[:200]})

    parent_pid = _find_codex_parent_pid()
    watcher_pidfile = _EXIT_WATCHERS_DIR / f"{parent_pid}.pid"
    try:
        if watcher_pidfile.exists():
            existing = int(watcher_pidfile.read_text(encoding="utf-8").strip())
            if _pid_alive(existing):
                hook_log(
                    "exit_watcher_already_running",
                    {"parent_pid": parent_pid, "pidfile": str(watcher_pidfile)},
                )
                return
    except Exception:
        pass

    bootstrap = {
        "parent_pid": parent_pid,
        "session_id": session_id,
        "dataset": dataset,
        "pidfile": str(watcher_pidfile),
    }
    log_path = _STATE_DIR / "exit-watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _EXIT_WATCHERS_DIR.mkdir(parents=True, exist_ok=True)
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
        hook_log(
            "exit_watcher_started",
            {
                "parent_pid": parent_pid,
                "session_id": session_id,
                "dataset": dataset,
                "pidfile": str(watcher_pidfile),
            },
        )
    except Exception as e:
        hook_log("exit_watcher_launch_failed", {"error": str(e)[:300]})


def _write_resolved(
    session_id: str,
    dataset: str,
    user_id: str,
    cwd: str,
    service_url: str = "",
    api_key: str = "",
    agent_id: str = "",
    agent_name: str = "",
    registered: bool = False,
) -> None:
    """Cache resolved session ID, dataset, user ID, and API key for other hook scripts."""
    _RESOLVED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "cwd": cwd,
        "registered": bool(registered),
    }
    if service_url:
        data["service_url"] = _normalize_service_url(service_url)
    if api_key:
        data["api_key"] = api_key
    if agent_id:
        data["agent_id"] = agent_id
    if agent_name:
        data["agent_name"] = agent_name
    _RESOLVED_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _start(payload: dict | None = None) -> dict:
    config = load_config()
    payload = payload or {}
    cwd = str(payload.get("cwd") or os.environ.get("CODEX_CWD") or os.getcwd())

    try:
        _ensure_local_server_running(config)
    except Exception as exc:
        hook_log("server_bootstrap_warning", {"error": str(exc)[:200]})

    session_id = get_session_id(config, cwd)
    dataset = get_dataset(config)

    # Configure cognee (cloud or local)
    try:
        await ensure_cognee_ready(config)
    except Exception as e:
        print(f"cognee-plugin: init warning ({e})", file=sys.stderr)

    # Register agent identity.
    user_id = ""
    agent_api_key = ""
    agent_id = ""
    agent_name = _resolve_agent_name(config, cwd)
    registered = False

    # Preferred HTTP path: create/get named agent, use its API key,
    # and register this session in agent-mode.
    if is_cloud_mode(config):
        try:
            (
                agent_id,
                agent_api_key,
                agent_name,
                registered,
            ) = await _ensure_agent_credentials_and_register(config, cwd, session_id)
            if agent_id:
                user_id = agent_id
        except Exception as exc:
            message = str(exc)[:300]
            hook_log("agent_lifecycle_error", {"error": message})
            print(f"cognee-plugin: agent lifecycle failed ({message})", file=sys.stderr)
            return {}
    else:
        # Local SDK fallback path.
        try:
            if not user_id:
                user_id, fallback_key = await ensure_identity(config)
                if fallback_key and not agent_api_key:
                    agent_api_key = fallback_key
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
    _write_resolved(
        session_id,
        dataset,
        user_id,
        cwd,
        service_url=str(config.get("service_url", "") or ""),
        api_key=agent_api_key,
        agent_id=agent_id,
        agent_name=agent_name,
        registered=registered,
    )

    # Create config file on first run if it doesn't exist
    config_file = Path.home() / ".cognee-plugin" / "config.json"
    if not config_file.exists():
        save_config(config)

    # Reset the idle clock for this Codex process before the watcher
    # starts, otherwise a stale timestamp from a prior session can cause
    # an immediate improve on startup.
    touch_activity()

    # Launch the idle watcher. If COGNEE_IDLE_DISABLED is set, skip it.
    if not config.get("service_url") and os.environ.get("COGNEE_IDLE_DISABLED", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
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
