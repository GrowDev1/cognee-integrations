# Cognee Memory Plugin for Claude Code

Gives Claude Code persistent memory across sessions using Cognee's knowledge graph. Tool calls and responses are automatically captured into session memory, relevant context is injected on every prompt, and session data is bridged into the permanent knowledge graph at session end.

## Install

### 1. Install Cognee

```bash
pip install cognee
```

### 2. Configure

**Local mode** (cognee runs in-process, no server needed):

```bash
export LLM_API_KEY="your-openai-key"
export CACHING=true   # required for session memory
```

**Backend mode** (connect to a local or remote Cognee API server):

```bash
export COGNEE_SERVICE_URL="http://localhost:8000"   # or your cloud URL
export COGNEE_API_KEY="your-api-key"                # optional if auth is disabled
export CACHING=true
```

On first start, the plugin auto-registers as `claude-code@cognee.agent` on the backend — it creates an agent user, logs in, obtains an API key, and reconnects with agent-specific credentials. The agent then appears in the Cognee UI's agents list.

If you already have an agent API key, set `COGNEE_API_KEY` directly and the plugin will use it without re-registering.

**Cognee Cloud**:

```bash
export COGNEE_SERVICE_URL="https://your-instance.cognee.ai"
export COGNEE_API_KEY="ck_..."
```

Or create `~/.cognee-plugin/config.json`:

```json
{
  "service_url": "http://localhost:8000",
  "dataset": "claude_sessions"
}
```

### 3. Enable the plugin

**Option A — permanent (recommended):**

Add the plugin directory to your shell profile so it loads on every session:

```bash
# Add to ~/.zshrc or ~/.bashrc
alias claude="claude --plugin-dir /path/to/cognee-integrations/integrations/claude-code"
```

Then reload: `source ~/.zshrc`

**Option B — single session:**

```bash
claude --plugin-dir /path/to/cognee-integrations/integrations/claude-code
```

**Option C — validate first:**

```bash
claude plugin validate /path/to/cognee-integrations/integrations/claude-code
```

When the plugin loads, you'll see "Cognee Memory Connected" with the mode, dataset, and session ID at the start of your session.

## How it works

The plugin hooks into six Claude Code lifecycle events:

| Hook | What it does |
|------|-------------|
| **SessionStart** | Loads config, computes a per-directory session ID, connects to Cognee Cloud if configured |
| **UserPromptSubmit** | Searches the session cache for context relevant to your prompt and injects it (3s timeout, fails silently) |
| **PostToolUse** | Captures tool name, input, and output into the session cache with `[category:agent]` tag (async, non-blocking) |
| **Stop** | Captures the final assistant response when you interrupt |
| **PreCompact** | Before context window compaction, builds a memory anchor from session + graph context so key knowledge survives the reset |
| **SessionEnd** | Runs `cognee.improve()` to bridge session data into the permanent knowledge graph |

## Data categories

The plugin organizes knowledge into three categories via `node_set` tagging:

| Category | Node set | What belongs here |
|----------|----------|-------------------|
| **user** | `user_context` | User preferences, corrections, personal facts |
| **project** | `project_docs` | Repository docs, code context, architecture decisions |
| **agent** | `agent_actions` | Tool call logs, reasoning traces (auto-captured by hooks) |

When using `/cognee-memory:cognee-remember`, Claude routes data to the correct category based on context. When searching with `/cognee-memory:cognee-search`, you can filter by category using `--node-set`.

## Session naming

Sessions are scoped per working directory by default. The session ID is derived from a prefix + directory name + hash:

```
cc_my-project_a1b2c3d4e5f6
```

You can change the strategy via config or env vars:

| Strategy | Env var | Behavior |
|----------|---------|----------|
| `per-directory` (default) | `COGNEE_SESSION_STRATEGY=per-directory` | One session per project directory |
| `git-branch` | `COGNEE_SESSION_STRATEGY=git-branch` | Includes git branch in session ID |
| `static` | `COGNEE_SESSION_ID=my-session` | Fixed session ID (legacy compat) |

## Skills

Three skills are available as slash commands:

- **`/cognee-memory:cognee-remember`** — permanently store data in the knowledge graph (full add + cognify + improve pipeline). Routes to user/project/agent category.
- **`/cognee-memory:cognee-search`** — explicitly search session or graph memory, optionally filtered by category. Automatic search happens on every prompt via hooks.
- **`/cognee-memory:cognee-sync`** — force-sync session data to the permanent graph without waiting for session end

## Status line (optional)

Adds a one-line status display at the bottom of your terminal showing cognee mode/dataset/session, recall hit counts from the most recent prompt, and saves accumulated for the current turn.

The script ships in the plugin at `scripts/cognee-statusline.sh`. Claude Code's `statusLine` setting is per-user, so you wire it into `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/absolute/path/to/cognee-integrations/integrations/claude-code/scripts/cognee-statusline.sh"
  }
}
```

Example output:

```
cognee[local] ds=claude_sessions sess=74f2b7ad530a | 🔍 recall: 5s/5t/1g | saving: 1p/0t/1a
```

The script reads three small JSON state files written by the plugin:

| File | Source | Surfaces |
|---|---|---|
| `~/.cognee-plugin/resolved.json` | SessionStart hook | mode, dataset, short session id |
| `~/.cognee-plugin/last_recall.json` | UserPromptSubmit hook | session/trace/graph_context hit counts |
| `~/.cognee-plugin/save_counter.json` | per-turn save hooks | prompt/trace/answer save counts (resets each prompt) |

Any missing piece is silently omitted, so the line stays short on idle turns.

## Auto-clear demo hook

For demos where each response should leave Claude with an empty transcript context, enable the Stop-hook clear path:

```bash
export COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE=true
claude --plugin-dir /path/to/cognee-integrations/integrations/claude-code
```

When enabled, the plugin's `Stop` hook empties Claude Code's `transcript_path` after each assistant response. Memory capture runs first, so the response can still be stored in Cognee before the local Claude context is deleted.

This is a demo-only workaround. Claude Code hooks do not expose a supported action for executing built-in slash commands like `/clear`, so the hook clears the backing transcript file directly.

## Audit log

The UserPromptSubmit hook also appends a JSONL entry per prompt to `~/.cognee-plugin/recall-audit.log`, recording the timestamp, session_id, prompt text, hit counts, and the full `additionalContext` injected into Claude's input. This is the source of truth for "what did the plugin give Claude on prompt X" — Claude Code does not persist hook `additionalContext` into its JSONL transcript.

```bash
tail -n 1 ~/.cognee-plugin/recall-audit.log | jq -r .context
```

## Configuration reference

| Key | Env var | Default | Description |
|-----|---------|---------|-------------|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `claude_sessions` | Dataset name for permanent storage |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | Session naming strategy |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `cc` | Prefix for session IDs |
| `service_url` | `COGNEE_SERVICE_URL` | -- | Cognee Cloud URL |
| `api_key` | `COGNEE_API_KEY` | -- | Cognee Cloud API key |
| `llm_api_key` | `LLM_API_KEY` | -- | LLM provider key (local mode) |
| `llm_model` | `LLM_MODEL` | -- | LLM model name (local mode) |
| demo auto-clear | `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | disabled | When truthy, empties Claude transcript context after each assistant response |
| `top_k` | -- | `5` | Results returned by automatic session search (per scope) |

Config is resolved in order: env vars > `~/.cognee-plugin/config.json` > defaults.
