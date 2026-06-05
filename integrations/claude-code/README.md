# Cognee Memory Plugin for Claude Code

Adds persistent memory to Claude Code through Cognee.

The integration:
- captures prompts, tool traces, and assistant responses into session memory
- injects relevant context on prompt submit
- syncs session memory into graph memory on session end/final exit

## Install

### 1. Install Cognee

```bash
pip install cognee
```

### 2. Configure runtime mode

The integration has two runtime modes.

1. `managed_endpoint`
- Used only when both `COGNEE_SERVICE_URL` and `COGNEE_API_KEY` are set.
- This covers Cognee Cloud and existing local/remote Cognee API servers.

```bash
export COGNEE_SERVICE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

2. `integration_local`
- Used when endpoint URL/key are not both set.
- Integration bootstraps local Cognee API (default `http://localhost:8011`).

You can also set config in `~/.cognee-plugin/config.json`:

```json
{
  "service_url": "https://your-instance.cognee.ai",
  "dataset": "claude_sessions",
  "agent_name": "my-agent"
}
```

### 3. Enable plugin

```bash
claude --plugin-dir /path/to/cognee-integrations/integrations/claude-code
```

Optional alias:

```bash
alias claude="claude --plugin-dir /path/to/cognee-integrations/integrations/claude-code"
```

On startup you should see a "Cognee Memory Connected" system message.

## Mode selection rules

At startup (`SessionStart`):
- both `COGNEE_SERVICE_URL` and `COGNEE_API_KEY` -> `managed_endpoint`
- otherwise -> `integration_local` (local API bootstrap)

At hook runtime:
- hooks resolve mode through runtime endpoint auth (env + `agent_keys.json`), not only config intent
- `http` mode skips local SDK initialization
- `local_sdk` mode runs `ensure_cognee_ready(...)`

The hooks emit `mode_decision` logs with:
- `mode`
- `service_url`
- `url_source`
- `key_source`
- `api_key_present`

## Agent lifecycle

In endpoint mode, SessionStart uses named-agent lifecycle:
- resolves effective agent name from `COGNEE_AGENT_NAME` or config
- normalizes name to Claude suffix (`*_claude`)
- validates cached key
- recreates agent when cached key is stale
- registers session connection

Files used:
- `~/.cognee-plugin/agent_keys.json`
- `~/.cognee-plugin/agent_keys.lock`

`agent_keys.json` stores per `(service_url, agent_name)` credentials.

## Hooks

| Hook | Behavior |
|---|---|
| `SessionStart` | mode select, identity/agent setup, dataset readiness, watcher bootstrap |
| `UserPromptSubmit` | context lookup + async prompt staging |
| `PostToolUse` | async trace write |
| `Stop` | assistant answer write + optional transcript clear hook |
| `PreCompact` | memory anchor build before compaction |
| `SessionEnd` | trigger detached final sync worker |

Claude-specific contracts are preserved:
- `hookSpecificOutput` payload format
- async hook behavior for write hooks

## Session sync and watchers

Final sync can be triggered by:
- `SessionEnd` detached worker path
- exit watcher fallback when process exits

To avoid duplicate final sync:
- detached workers claim one-shot markers in `~/.cognee-plugin/final-sync-once/*.done`
- stale markers are pruned with TTL of 1 hour

Final detached sync also performs unregister-on-finish when applicable.

## Skills

- `/cognee-memory:cognee-remember`
- `/cognee-memory:cognee-search`
- `/cognee-memory:cognee-sync`

## Status line (optional)

Configure Claude status line command:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/absolute/path/to/cognee-integrations/integrations/claude-code/scripts/cognee-statusline.sh"
  }
}
```

The status line reads:
- `~/.cognee-plugin/last_recall.json`
- `~/.cognee-plugin/save_counter.json`

Runtime session/dataset/API readiness are resolved from endpoint calls and `agent_keys.json` fallback logic.

## Auto-clear demo hook

For demo flows where each response should clear local transcript context:

```bash
export COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE=true
```

This clears the transcript file on `Stop` after memory capture.

## Breaking changes and migration notes

- `resolved.json` is no longer used.
- Hook-time routing is runtime-auth driven (`http` vs `local_sdk`).
- Session-end sync uses detached workers + dedupe markers.

## Troubleshooting

1. Unauthorized / stale key behavior
- Check `~/.cognee-plugin/agent_keys.json`.
- If key is stale, integration should invalidate cache and recreate agent.
- Relevant logs: `agent_key_stale_detected`, `agent_register_result`, `agent_lifecycle_error`.

2. Missing session key
- If payload session key is missing, SessionStart refuses registration.
- Relevant logs: `session_key_resolved`, `missing_payload_session_id`.

3. Final sync diagnostics
- Check `~/.cognee-plugin/hook.log` and `~/.cognee-plugin/exit-watcher.log`.
- Relevant logs: `sync_deferred_to_shutdown_worker`, `final_sync_once_*`, `agent_unregister_result`.

## Configuration reference

Config precedence:
1. env vars
2. `~/.cognee-plugin/config.json`
3. defaults

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_CLAUDE_DATASET`, `COGNEE_CODEX_DATASET`, `COGNEE_PLUGIN_DATASET` | `claude_sessions` | Dataset name |
| `agent_name` | `COGNEE_AGENT_NAME` | `claude-code-agent` | Base name, normalized to Claude suffix |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `cc` | Session ID prefix |
| `_static_session_id` | `COGNEE_SESSION_ID` | unset | Legacy static session ID |
| `backend` | `COGNEE_CLAUDE_BACKEND`, `COGNEE_CODEX_BACKEND` | `auto` | Legacy/compat backend override |
| `service_url` | `COGNEE_SERVICE_URL` | unset | Must pair with API key for managed endpoint |
| `api_key` | `COGNEE_API_KEY` | unset | Must pair with service URL for managed endpoint |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` fallback | Legacy/internal compatibility override |
| user login | `COGNEE_USER_EMAIL`, `COGNEE_USER_PASSWORD` | defaults in config | Used for owner bootstrap when needed |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Local mode SDK/runtime settings |
| demo auto-clear | `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | disabled | Stop hook transcript clear |

