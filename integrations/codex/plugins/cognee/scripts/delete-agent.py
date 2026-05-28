#!/usr/bin/env python3
"""Delete a Cognee agent by name and remove local cached credentials."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log
from config import load_config

_STATE_DIR = Path.home() / ".cognee-plugin" / "codex"
_AGENT_KEYS_CACHE = _STATE_DIR / "agent_keys.json"
_RESOLVED_CACHE = _STATE_DIR / "resolved.json"
_DEFAULT_SERVICE_URL = "http://localhost:8011"


def _normalize_service_url(service_url: str) -> str:
    return str(service_url or "").strip().rstrip("/")


def _agent_cache_key(service_url: str, agent_name: str) -> str:
    return f"{_normalize_service_url(service_url)}::{agent_name}"


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


def _clear_resolved_if_matches(service_url: str, agent_name: str) -> None:
    try:
        if not _RESOLVED_CACHE.exists():
            return
        resolved = json.loads(_RESOLVED_CACHE.read_text(encoding="utf-8"))
        if not isinstance(resolved, dict):
            return
        if (
            _normalize_service_url(resolved.get("service_url", ""))
            == _normalize_service_url(service_url)
            and str(resolved.get("agent_name", "") or "") == agent_name
        ):
            resolved.pop("agent_id", None)
            resolved.pop("api_key", None)
            resolved["registered"] = False
            _RESOLVED_CACHE.write_text(json.dumps(resolved, indent=2), encoding="utf-8")
    except Exception as exc:
        hook_log("resolved_cache_clear_failed", {"error": str(exc)[:200]})


async def _get_owner_api_key(service_url: str) -> str:
    import aiohttp

    email = os.environ.get("DEFAULT_USER_EMAIL", "default_user@example.com")
    password = os.environ.get("DEFAULT_USER_PASSWORD", "default_password")
    base = _normalize_service_url(service_url)

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


async def _delete_agent(service_url: str, agent_name: str) -> None:
    import aiohttp

    base = _normalize_service_url(service_url)
    owner_key = await _get_owner_api_key(base)

    headers = {"X-Api-Key": owner_key}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{base}/api/v1/agents/") as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"list agents failed ({resp.status}: {body[:200]})")
            agents = await resp.json()

        target = None
        for item in agents if isinstance(agents, list) else []:
            email = str(item.get("agentEmail", "") or item.get("agent_email", "")).strip()
            short_name = email[:-13] if email.endswith("@cognee.agent") else email
            if short_name == agent_name:
                target = str(item.get("agentId", "") or item.get("agent_id", ""))
                break

        if not target:
            raise RuntimeError(
                f"Agent '{agent_name}' not found on {base}. "
                "If cache is stale, remove local entry manually."
            )

        async with session.delete(f"{base}/api/v1/agents/{target}") as resp:
            if resp.status not in (200, 204):
                body = await resp.text()
                raise RuntimeError(f"delete agent failed ({resp.status}: {body[:200]})")

    cache = _load_agent_keys_cache()
    entries = cache.get("entries", {})
    cache_key = _agent_cache_key(base, agent_name)
    if isinstance(entries, dict):
        entries.pop(cache_key, None)
        cache["entries"] = entries
        _save_agent_keys_cache(cache)
    _clear_resolved_if_matches(base, agent_name)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete a Cognee agent and clear local cache entry."
    )
    parser.add_argument(
        "--agent-name", default="", help="Agent name (defaults to config agent_name)."
    )
    parser.add_argument(
        "--service-url", default="", help="Service URL (defaults to config/env/local)."
    )
    args = parser.parse_args()

    config = load_config()
    agent_name = str(args.agent_name or config.get("agent_name") or "").strip()
    if not agent_name:
        print("cognee-plugin: missing agent name", file=sys.stderr)
        return 2

    service_url = _normalize_service_url(
        str(
            args.service_url
            or config.get("service_url")
            or os.environ.get("COGNEE_SERVICE_URL")
            or _DEFAULT_SERVICE_URL
        )
    )

    try:
        asyncio.run(_delete_agent(service_url, agent_name))
        print(
            f"cognee-plugin: deleted agent '{agent_name}' on {service_url}"
            f" and cleared local cache entry",
            file=sys.stderr,
        )
        return 0
    except Exception as exc:
        message = str(exc)[:300]
        hook_log(
            "delete_agent_failed",
            {"service_url": service_url, "agent_name": agent_name, "error": message},
        )
        print(f"cognee-plugin: delete agent failed ({message})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
