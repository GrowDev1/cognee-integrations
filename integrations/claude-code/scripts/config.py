"""Shared configuration for the Cognee Claude Code plugin.

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
_CONFIG_FILE = _CONFIG_DIR / "config.json"

_DEFAULTS = {
    "dataset": "claude_sessions",
    "session_strategy": "per-directory",  # per-directory | git-branch | static
    "session_prefix": "cc",
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

# Env var overrides (env var name → config key)
_ENV_MAP = {
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
        except Exception:
            pass

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
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())

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
    return config.get("dataset", "claude_sessions")


def is_cloud_mode(config: dict) -> bool:
    """Check if cloud/remote mode is configured."""
    return bool(config.get("service_url"))


def is_local_mode(config: dict) -> bool:
    """Check if local mode (has LLM key, no cloud URL)."""
    return bool(config.get("llm_api_key")) and not is_cloud_mode(config)


_AGENT_EMAIL = "claude-code@cognee.agent"
_AGENT_PASSWORD = "claude-code-agent"


async def ensure_identity(config: dict):
    """Register the Claude Code agent with Cognee and obtain an API key.

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
                            # Reconnect serve() with agent's own API key
                            import cognee

                            await cognee.disconnect()
                            await cognee.serve(url=service_url, api_key=agent_key)
                            print(
                                f"cognee-plugin: connected as agent (key={agent_key[:8]}...)",
                                file=sys.stderr,
                            )
                            return _get_user_id_from_jwt(jwt), agent_key
        except Exception:
            pass

        # 4. Create API key for agent
        try:
            async with session.post(
                f"{base}/api/v1/auth/api-keys",
                json={"name": "claude-code-plugin"},
                cookies={"auth_token": jwt},
            ) as resp:
                if resp.status == 200:
                    key_data = await resp.json()
                    agent_key = key_data["key"]
                    # Reconnect serve() with agent's own API key
                    import cognee

                    await cognee.disconnect()
                    await cognee.serve(url=service_url, api_key=agent_key)
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


_RESOLVED_CACHE_PATH = Path.home() / ".cognee-plugin" / "resolved.json"
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
    import cognee

    if is_cloud_mode(config):
        url = config["service_url"]
        # Try config first, then fall back to cached key from SessionStart
        api_key = config.get("api_key", "")
        if not api_key and _RESOLVED_CACHE_PATH.exists():
            try:
                resolved = json.loads(_RESOLVED_CACHE_PATH.read_text(encoding="utf-8"))
                api_key = resolved.get("api_key", "")
            except Exception:
                pass
        kwargs = {"url": url}
        if api_key:
            kwargs["api_key"] = api_key
        await cognee.serve(**kwargs)
        print(f"cognee-plugin: connected to {url}", file=sys.stderr)
        return

    if config.get("llm_api_key"):
        cognee.config.set_llm_api_key(config["llm_api_key"])
    if config.get("llm_model"):
        cognee.config.set_llm_model(config["llm_model"])

    await _ensure_local_databases()
    print("cognee-plugin: local databases ready", file=sys.stderr)


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
    except Exception:
        pass
    return ""
