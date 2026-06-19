---
name: cognee-configure-session
description: Switch which Cognee session this terminal uses. Lists existing sessions and lets the user pick one (by id), then routes the rest of the conversation's memory into that session.
---

# Switch the active Cognee session

Use this when the user wants to see, choose, or switch the Cognee memory session
the current terminal writes to and recalls from (e.g. "switch session", "resume a
past session", "what sessions exist", "/cognee-configure-session").

## Steps

1. **List sessions:**
   ```bash
   python3 "${PLUGIN_ROOT}/scripts/cognee-session-list.py"
   ```
   This prints JSON: `{ "current": "<current session id>", "total": N,
   "sessions": [ { "session_id", "last_activity_at", "status", "last_model" }, ... ] }`
   (most-recent first).

2. **Show the user the options.** Render a short, readable, numbered list of the
   sessions (id + last activity + status), and clearly mark which one is `current`.
   Highlight the 2–3 most recently used.

3. **Ask the user which to use.** Codex has no interactive picker, so ask in plain
   text: have the user reply with a session id from the list, or a brand-new name
   to start a fresh session. (If the user already passed a session id as the
   command argument, skip the prompt and use it.)

4. **Apply the pick** with the chosen (or typed) session id:
   ```bash
   python3 "${PLUGIN_ROOT}/scripts/cognee-session-set.py" "<chosen-session-id>"
   ```
   It prints `{ "ok": true, "old_session", "new_session", "switched", "existed",
   "created", "synced_previous" }`. Note `new_session` may be a sanitized form of
   what the user typed — report the value the script returns.

5. **Confirm.** Tell the user the active session is now `<new_session>`:
   - If `created` is true (the id didn't exist), say you **started a new session**
     named `<new_session>` — a typed name that doesn't match an existing session
     creates a fresh one.
   - Otherwise say you **resumed** `<new_session>`.
   - If `synced_previous` is true, mention the previous session's memory was
     flushed to the graph.
   From the next message on, recall and saving happen in the new session — no
   restart needed.

## Notes

- The switch affects **only this terminal**. Other running agents keep their own
  sessions. Two terminals can deliberately pick the *same* session id to share one.
- If `cognee-session-set.py` returns `ok: false` with "could not determine current
  launch", the picker couldn't identify this terminal; report that rather than
  guessing.
