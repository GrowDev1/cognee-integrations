# Cognee Codex Plugin

This directory is a local Codex plugin marketplace for Cognee. The plugin uses
the installed `cognee` Python package by default, captures Codex session events
into Cognee session memory, recalls relevant memory on each prompt, and syncs
session memory into Cognee's graph during compaction, supported session-end
events, idle periods, or after the owning Codex process exits.

## Install

Create or activate the Python environment where `cognee` is installed:

```bash
cd /path/to/your/project
python3 -m venv .venv
source .venv/bin/activate
python -m pip install cognee
```

Enable Codex hooks in `~/.codex/config.toml`:

```toml
[features]
hooks = true
plugin_hooks = true
```

Add this local marketplace and install the plugin:

```bash
cd /path/to/cognee-integrations/integrations/codex
codex plugin marketplace add .
codex plugin add cognee@cognee-local
```

Start Codex from the same environment where `cognee` is installed:

```bash
cd /path/to/your/project
source .venv/bin/activate
codex
```

## Configuration

Native local mode is the default. To require it explicitly:

```bash
export COGNEE_CODEX_BACKEND=native
```

Graph sync uses Cognee LLM configuration, so set the LLM API key expected by
your Cognee install before running compaction or graph sync:

```bash
export LLM_API_KEY="your-key"
```

Optional settings:

```bash
export COGNEE_CODEX_DATASET=codex_sessions
export COGNEE_CODEX_RECALL_SCOPE=session,trace,graph_context,graph
export COGNEE_IDLE_DISABLED=true
```

HTTP/API mode is still supported for hosted Cognee:

```bash
export COGNEE_CODEX_BACKEND=http
export COGNEE_SERVICE_URL=http://localhost:8000
export COGNEE_API_KEY="your-key"
```

## Update Or Remove

After editing this plugin, reinstall it so Codex refreshes the cached copy:

```bash
cd /path/to/cognee-integrations/integrations/codex
codex plugin remove cognee@cognee-local
codex plugin add cognee@cognee-local
```

To remove the marketplace too:

```bash
codex plugin remove cognee@cognee-local
codex plugin marketplace remove cognee-local
```

## Logs And State

Plugin state and hook logs are written under:

```bash
~/.cognee-plugin/codex/
```

Useful files:

```bash
tail -f ~/.cognee-plugin/codex/hook.log
tail -f ~/.cognee-plugin/codex/subprocess.log
tail -f ~/.cognee-plugin/codex/recall-audit.log
tail -f ~/.cognee-plugin/codex/exit-watcher.log
```

## What The Hooks Do

- `SessionStart`: resolves session, dataset, user, starts idle and exit watchers.
- `UserPromptSubmit`: recalls session, trace, graph context, and graph memory.
- `PostToolUse`: stores tool calls as Cognee trace entries.
- `Stop`: stores the assistant response paired with the pending user prompt.
- `PreCompact`: emits a compact session/trace memory anchor and starts graph sync.
- `SessionEnd`: starts graph sync when the Codex client dispatches this hook.
