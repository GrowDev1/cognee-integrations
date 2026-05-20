"""Shared configuration for the Cognee Codex plugin.

Loads settings from (in priority order):
  1. Environment variables (runtime overrides)
  2. Config file (~/.cognee-plugin/config.json)
  3. Defaults

Config file is created on first SessionStart if it doesn't exist.

Supports three modes:
  - Local: Cognee runs in-process (SQLite + LanceDB + Kuzu)
  - Cloud: Connect to Cognee Cloud via cognee.serve()
  - Server: Legacy — direct base_url (kept for backward compat)
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_CONFIG_DIR = Path.home() / ".cognee-plugin"
_STATE_DIR = _CONFIG_DIR / "codex"
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_BRIDGE_STATE_FILE = _STATE_DIR / "bridge_state.json"
_HOOK_LOG = _STATE_DIR / "hook.log"

_DEFAULTS = {
    "dataset": "codex_sessions",
    "session_strategy": "per-directory",  # per-directory | git-branch | static
    "session_prefix": "codex",
    "top_k": 3,
    # Cloud / remote
    "service_url": "",
    "api_key": "",
    # Local mode
    "llm_api_key": "",
    "llm_model": "",
    # Legacy server mode
    "base_url": "",
}


def _config_log(event: str, detail: dict | None = None) -> None:
    try:
        from datetime import datetime, timezone

        _HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "event": event,
        }
        if detail:
            line["detail"] = detail
        with _HOOK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass

# Env var overrides (env var name → config key)
_ENV_MAP = {
    "COGNEE_CODEX_DATASET": "dataset",
    "COGNEE_PLUGIN_DATASET": "dataset",
    "COGNEE_SESSION_STRATEGY": "session_strategy",
    "COGNEE_SESSION_PREFIX": "session_prefix",
    "COGNEE_SERVICE_URL": "service_url",
    "COGNEE_API_KEY": "api_key",
    "COGNEE_BASE_URL": "base_url",
    "LLM_API_KEY": "llm_api_key",
    "LLM_MODEL": "llm_model",
    # Legacy compat
    "COGNEE_SESSION_ID": "_static_session_id",
}


def load_config() -> dict:
    """Load merged config: defaults → file → env vars."""
    config = dict(_DEFAULTS)

    # Layer 2: config file
    if _CONFIG_FILE.exists():
        try:
            file_cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items() if v is not None and v != ""})
        except Exception as exc:
            _config_log("config_file_load_failed", {"path": str(_CONFIG_FILE), "error": str(exc)[:200]})

    # Layer 3: env vars (highest priority)
    for env_key, config_key in _ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            config[config_key] = val

    return config


def save_config(config: dict) -> None:
    """Write config to disk. Creates directory if needed."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Only save non-secret, non-default values
    to_save = {
        k: v for k, v in config.items() if not k.startswith("_") and v and v != _DEFAULTS.get(k)
    }
    _CONFIG_FILE.write_text(json.dumps(to_save, indent=2), encoding="utf-8")


def get_session_id(config: dict, cwd: Optional[str] = None) -> str:
    """Compute session ID based on the configured strategy.

    Strategies:
      - per-directory: prefix + hash of cwd → stable per-project
      - git-branch: prefix + hash of cwd + branch → stable per-branch
      - static: uses COGNEE_SESSION_ID env var or fallback
    """
    # Legacy: explicit static session ID
    static_id = config.get("_static_session_id", "")
    if static_id:
        return static_id

    strategy = config.get("session_strategy", "per-directory")
    prefix = config.get("session_prefix", "cc")

    if cwd is None:
        cwd = os.environ.get("CODEX_CWD", os.getcwd())

    if strategy == "static":
        return f"{prefix}_session"

    # Per-directory: hash the cwd for a stable, short ID
    dir_hash = hashlib.sha256(cwd.encode()).hexdigest()[:12]
    dir_name = Path(cwd).name

    if strategy == "git-branch":
        branch = _get_git_branch(cwd)
        if branch:
            return f"{prefix}_{dir_name}_{branch}_{dir_hash}"

    return f"{prefix}_{dir_name}_{dir_hash}"


def get_dataset(config: dict) -> str:
    """Get the dataset name from config."""
    return config.get("dataset", "codex_sessions")


def is_cloud_mode(config: dict) -> bool:
    """Check if cloud/remote mode is configured."""
    return bool(config.get("service_url"))


def is_local_mode(config: dict) -> bool:
    """Check if local mode (has LLM key, no cloud URL)."""
    return bool(config.get("llm_api_key")) and not is_cloud_mode(config)


_AGENT_EMAIL = "codex@cognee.agent"
_AGENT_PASSWORD = "codex-agent"


async def ensure_identity(config: dict):
    """Register the Codex agent with Cognee and obtain an API key.

    When connected to a backend (service_url is set), registers via the
    HTTP API using the @cognee.agent email pattern so the agent appears
    in the agents list. Creates an agent-specific API key and reconnects
    cognee.serve() with it.

    In local SDK mode (no service_url), falls back to creating a user
    via the SDK directly.

    Returns (user_id, api_key) tuple. api_key may be empty in local mode.
    """
    service_url = config.get("service_url", "")

    if service_url:
        return await _ensure_identity_via_api(service_url, config)
    else:
        user_id = await _ensure_identity_via_sdk()
        return user_id, ""


async def _ensure_identity_via_api(service_url: str, config: dict) -> tuple:
    """Register agent via the backend HTTP API. Returns (user_id, api_key)."""
    import aiohttp

    base = service_url.rstrip("/")

    async with aiohttp.ClientSession() as session:
        # 1. Register agent user (idempotent — 400 if exists)
        try:
            async with session.post(
                f"{base}/api/v1/auth/register",
                json={
                    "email": _AGENT_EMAIL,
                    "password": _AGENT_PASSWORD,
                    "is_verified": True,
                },
            ) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    print(
                        f"cognee-plugin: registered agent {_AGENT_EMAIL} (id={data['id']})",
                        file=sys.stderr,
                    )
                elif resp.status in (400, 409):
                    print(
                        f"cognee-plugin: agent {_AGENT_EMAIL} already registered", file=sys.stderr
                    )
                else:
                    text = await resp.text()
                    print(
                        f"cognee-plugin: register warning ({resp.status}: {text})", file=sys.stderr
                    )
        except Exception as e:
            print(f"cognee-plugin: register failed ({e})", file=sys.stderr)

        # 2. Login to get JWT
        try:
            async with session.post(
                f"{base}/api/v1/auth/login",
                data={"username": _AGENT_EMAIL, "password": _AGENT_PASSWORD},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status != 200:
                    print(f"cognee-plugin: agent login failed ({resp.status})", file=sys.stderr)
                    return "", ""
                login_data = await resp.json()
                jwt = login_data["access_token"]
        except Exception as e:
            print(f"cognee-plugin: agent login failed ({e})", file=sys.stderr)
            return "", ""

        # 3. Check if agent already has an API key
        try:
            async with session.get(
                f"{base}/api/v1/auth/api-keys",
                cookies={"auth_token": jwt},
            ) as resp:
                if resp.status == 200:
                    keys = await resp.json()
                    if keys:
                        agent_key = keys[0].get("key", "")
                        if agent_key:
                            print(
                                f"cognee-plugin: connected as agent (key={agent_key[:8]}...)",
                                file=sys.stderr,
                            )
                            return _get_user_id_from_jwt(jwt), agent_key
        except Exception as exc:
            _config_log("agent_api_key_lookup_failed", {"error": str(exc)[:200]})

        # 4. Create API key for agent
        try:
            async with session.post(
                f"{base}/api/v1/auth/api-keys",
                json={"name": "codex-plugin"},
                cookies={"auth_token": jwt},
            ) as resp:
                if resp.status == 200:
                    key_data = await resp.json()
                    agent_key = key_data["key"]
                    print(
                        f"cognee-plugin: created agent API key (key={agent_key[:8]}...)",
                        file=sys.stderr,
                    )
                    return _get_user_id_from_jwt(jwt), agent_key
                else:
                    text = await resp.text()
                    print(
                        f"cognee-plugin: API key creation failed ({resp.status}: {text})",
                        file=sys.stderr,
                    )
        except Exception as e:
            print(f"cognee-plugin: API key creation failed ({e})", file=sys.stderr)

    return "", ""


async def ensure_dataset_ready_via_api(service_url: str, api_key: str, dataset: str) -> None:
    """Ensure the remote backend has the dataset for the authenticated agent.

    This mirrors local SDK mode's ``ensure_dataset_ready(dataset, user)``:
    the backend creates or returns the dataset and grants permissions to
    the API-key user.
    """
    if not service_url or not api_key or not dataset:
        return

    import aiohttp

    base = service_url.rstrip("/")
    async with aiohttp.ClientSession(headers={"X-Api-Key": api_key}) as session:
        async with session.post(f"{base}/api/v1/datasets", json={"name": dataset}) as resp:
            if resp.status in (200, 201):
                return
            text = await resp.text()
            raise RuntimeError(f"remote dataset ensure failed ({resp.status}: {text[:200]})")


def _get_user_id_from_jwt(jwt: str) -> str:
    """Extract user_id (sub claim) from JWT without verification."""
    import base64
    import json as _json

    try:
        payload = jwt.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = _json.loads(base64.urlsafe_b64decode(payload))
        return data.get("sub", "")
    except Exception:
        return ""


async def _ensure_identity_via_sdk() -> str:
    """Create agent identity via SDK (local mode, no backend)."""
    from cognee.modules.users.methods import create_user, get_user_by_email

    user = await get_user_by_email(_AGENT_EMAIL)
    if user:
        return str(user.id)

    try:
        user = await create_user(
            email=_AGENT_EMAIL,
            password=_AGENT_PASSWORD,
            is_verified=True,
            is_active=True,
        )
        print(f"cognee-plugin: created identity {_AGENT_EMAIL} (id={user.id})", file=sys.stderr)
        return str(user.id)
    except Exception:
        user = await get_user_by_email(_AGENT_EMAIL)
        if user:
            return str(user.id)
        return ""


_RESOLVED_CACHE_PATH = _STATE_DIR / "resolved.json"
_LOCAL_SETUP_DONE = False


async def _ensure_local_databases() -> None:
    """Create Cognee's local relational/vector stores for SDK mode."""
    global _LOCAL_SETUP_DONE
    if _LOCAL_SETUP_DONE:
        return

    from cognee.modules.engine.operations.setup import setup

    await setup()
    _LOCAL_SETUP_DONE = True


async def ensure_cognee_ready(config: dict) -> None:
    """Configure cognee for the active mode (cloud or local).

    In cloud mode, loads the cached API key from resolved.json (written
    by SessionStart) so that hooks running in separate processes can
    authenticate against the server.

    In local SDK mode, also runs Cognee's setup() so a fresh machine or
    fresh virtualenv has its databases/tables before identity, recall, or
    session writes touch them.
    """
    if is_cloud_mode(config):
        url = config["service_url"]
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{url.rstrip('/')}/health") as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"backend health check failed ({resp.status}: {text[:200]})")
        print(f"cognee-plugin: connected to {url}", file=sys.stderr)
        return

    import cognee

    if config.get("llm_api_key"):
        cognee.config.set_llm_api_key(config["llm_api_key"])
    if config.get("llm_model"):
        cognee.config.set_llm_model(config["llm_model"])

    await _ensure_local_databases()
    print("cognee-plugin: local databases ready", file=sys.stderr)


async def ensure_dataset_ready(dataset: str, user) -> None:
    """Ensure the user can write to the dataset before session bridging.

    On a fresh local install, session bridging can run before the dataset
    has been created, causing persistence to no-op with permission
    errors. Use Cognee's own pipeline resolver so dataset creation and
    ACL grants follow the SDK's normal path.

    Cognee 1.0.8's session/trace persistence pipelines call memify()
    without forwarding their user argument. In local plugin processes,
    make the resolved agent the process-local default user too, so those
    nested calls resolve the same write permissions.
    """
    from cognee.base_config import get_base_config
    from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
        resolve_authorized_user_datasets,
    )

    email = getattr(user, "email", "")
    if email:
        get_base_config().default_user_email = email

    await resolve_authorized_user_datasets(dataset, user=user)


async def sync_graph_context_to_session(dataset: str, session_id: str, user) -> dict:
    """Sync permanent graph context into one session without full improve().

    ``cognee.improve(session_ids=[...])`` also persists session cache
    entries. The integration already does that explicitly via
    ``persist_session_cache_to_graph()``, so call only Cognee's final
    graph-context sync step to keep recall working without duplicating
    session documents or holding the DB lock for the full improve path.
    """
    if not session_id or not user:
        return {"synced": 0}

    from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
        resolve_authorized_user_datasets,
    )
    from cognee.tasks.memify.sync_graph_to_session import sync_graph_to_session

    _, authorized_datasets = await resolve_authorized_user_datasets(dataset, user=user)
    if not authorized_datasets:
        return {"synced": 0}

    return await sync_graph_to_session(
        user_id=str(user.id),
        session_id=session_id,
        dataset_id=authorized_datasets[0].id,
        dataset_name=dataset,
    )


def _read_field(entry, field: str) -> str:
    if isinstance(entry, dict):
        return str(entry.get(field) or "")
    return str(getattr(entry, field, "") or "")


def _load_bridge_state() -> dict:
    try:
        return json.loads(_BRIDGE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        _config_log("bridge_state_load_failed", {"path": str(_BRIDGE_STATE_FILE), "error": str(exc)[:200]})
        return {}


def _save_bridge_state(state: dict) -> None:
    try:
        _BRIDGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BRIDGE_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        _config_log("bridge_state_save_failed", {"path": str(_BRIDGE_STATE_FILE), "error": str(exc)[:200]})


def _bridge_state_key(dataset: str, session_id: str, user_id: str, kind: str) -> str:
    return hashlib.sha256(f"{user_id}:{dataset}:{session_id}:{kind}".encode()).hexdigest()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def persist_session_cache_to_graph(dataset: str, session_id: str, user) -> bool:
    """Persist cached session QA/trace text to the permanent graph.

    This is a local-mode compatibility bridge for Cognee 1.0.8. The SDK's
    built-in session persistence can complete without extracting entries
    from the file-system cache in the plugin setup. Reading the same
    cache directly here keeps the integration useful while still using
    cognee.remember() for the actual add+cognify pipeline.
    """
    if not session_id or not user:
        return False

    import cognee
    from cognee.infrastructure.session.get_session_manager import get_session_manager

    await ensure_dataset_ready(dataset, user)

    user_id = str(user.id)
    session_manager = get_session_manager()
    if not session_manager.is_available:
        return False

    wrote = False
    bridge_state = _load_bridge_state()
    state_changed = False

    qa_entries = await session_manager.get_session(
        user_id=user_id,
        session_id=session_id,
        formatted=False,
    )
    qa_lines: list[str] = []
    for entry in qa_entries or []:
        question = _read_field(entry, "question").strip()
        answer = _read_field(entry, "answer").strip()
        if question:
            qa_lines.append(f"Question: {question}")
        if answer:
            qa_lines.append(f"Answer: {answer}")
        if question or answer:
            qa_lines.append("")
    qa_text = "\n".join(qa_lines).strip()
    if qa_text:
        qa_document = f"Session ID: {session_id}\n\n{qa_text}"
        qa_key = _bridge_state_key(dataset, session_id, user_id, "qa")
        qa_hash = _content_hash(qa_document)
        if bridge_state.get(qa_key) == qa_hash:
            qa_text = ""
        else:
            bridge_state[qa_key] = qa_hash
            state_changed = True

    if qa_text:
        await cognee.remember(
            qa_document,
            dataset_name=dataset,
            node_set=["user_sessions_from_cache"],
            self_improvement=False,
            run_in_background=False,
            user=user,
        )
        wrote = True

    trace_values = await session_manager.get_agent_trace_feedback(
        user_id=user_id,
        session_id=session_id,
    )
    trace_text = "\n".join(str(value).strip() for value in trace_values or [] if str(value).strip())
    if trace_text:
        trace_document = f"Session ID: {session_id}\n\n{trace_text}"
        trace_key = _bridge_state_key(dataset, session_id, user_id, "trace")
        trace_hash = _content_hash(trace_document)
        if bridge_state.get(trace_key) == trace_hash:
            trace_text = ""
        else:
            bridge_state[trace_key] = trace_hash
            state_changed = True

    if trace_text:
        await cognee.remember(
            trace_document,
            dataset_name=dataset,
            node_set=["agent_trace_feedbacks"],
            self_improvement=False,
            run_in_background=False,
            user=user,
        )
        wrote = True

    if state_changed:
        _save_bridge_state(bridge_state)

    return wrote


def _get_git_branch(cwd: str) -> str:
    """Get current git branch, or empty string if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            # Sanitize for use in session IDs
            return branch.replace("/", "-").replace(" ", "-")[:40]
    except Exception as exc:
        _config_log("git_branch_lookup_failed", {"cwd": cwd, "error": str(exc)[:200]})
    return ""
