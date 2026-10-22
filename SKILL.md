---
name: claudectx
description: "ClaudeCtx: keeps the conversation context small. At task boundaries Claude condenses the whole session into a core-context note and clears the session itself (or asks for /clear); the note reloads after /clear. Use when a ClaudeCtx hook message reports the context size or loads a note, when a task is done and the next request is unrelated, when the user says devam, continue, öz bağlam, temizle, or asks about token use, context size, handoff or skill cost."
---

# ClaudeCtx: one task per session, state in a core-context note

Every request re-sends the whole conversation, so cost grows with session length. `/clear` costs nothing; `/compact` is itself a large request. Keep each session to one task and carry state across `/clear` in a short core-context note.

## 1. Core context + clear (main rule)
Do this when a ClaudeCtx hook reported the soft limit and the current task is done, when it reported the hard limit and the current step is done, when a task is done and the user's next request is unrelated, or when the user asks for it (öz bağlam, temizle, handoff):
1. Never mid-edit. Leave the code consistent first.
2. Read the whole session, not only what compaction kept: `python "${CLAUDE_SKILL_DIR}/scripts/ctx_hook.py" digest --transcript "<transcript from the hook>"` prints every message the user typed. Without a hook message, leave out `--transcript`; the newest transcript is used. Take the user's rules from it verbatim.
3. Write the note to the handoff path the hook gave, or get it with `python "${CLAUDE_SKILL_DIR}/scripts/ctx_hook.py" path`. At most 40 lines, in this template:
   ```
   # Handoff YYYY-MM-DD HH:MM
   Goal: <one line>
   Status: <done, or what remains>
   Done: <changed files, one line each>
   User rules: <the user's constraints, verbatim>
   Decisions: <choices made and why, one line each>
   Open issues: <bugs, unverified assumptions>
   Next step: <the very next action; an unrelated request the user just made, verbatim>
   Key paths/commands: <build/test commands, files with line refs>
   ```
   No code dumps and no history narrative. Keep only what the next session cannot rediscover cheaply.
4. In your last message give the task's result, then one line: the context size, the note path, and that the session is being cleared.
5. Clear, as the very last action of the turn:
   - Claude desktop app: load `mcp__ccd_session_mgmt__clear_session` with ToolSearch if it is deferred, then call it with `session_id: "self"`. The app asks the user to approve. The clear happens when the turn ends, and nothing after the call carries over.
   - Without that tool (terminal, IDE), or when the hook said auto_clear is off: ask the user to run `/clear`.
   - If the user declines, continue here and do not offer again until the hook reports a higher level.

## 2. After the clear
The first session that starts after the note was written (after /clear, or the new session the desktop app's clear_session opens) gets the note in full if it is under 30 minutes old: that is the core context. Trust it, follow its User rules, continue from Next step when the user continues, and re-read only the files that step touches. An older note is only pointed to: read it if the user continues that work ("devam", "continue"), otherwise ignore it. When the note's task is finished, overwrite the note with the new state or delete it.

## 3. While working (slows context growth)
- Read large files by range (offset/limit) or with Grep. Do not re-read files that have not changed.
- Cap command output: tail/head, quiet flags, failures-only test output.
- Verbose jobs (full test runs, log analysis, broad searches): propose a subagent, so only its summary enters this context. Its own tokens still count.
- An unrelated question in a large session: suggest a new session instead of answering here.

## 4. Measuring and skill cost
- `python "${CLAUDE_SKILL_DIR}/scripts/ctx_report.py" --since YYYY-MM-DD [--clear-at 120000]`: context per request, sessions, idle cache misses, and a what-if estimate.
- `python "${CLAUDE_SKILL_DIR}/scripts/skill_audit.py"`: listing cost and usage of each skill, with a suggested `skillOverrides` block.

Report results as measured X -> Y and never promise percentages. Change settings.json (hooks, `skillOverrides`, `autoCompactWindow`) only after showing a diff and getting the user's explicit approval.
