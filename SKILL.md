---
name: claudectx
description: "ClaudeCtx: keeps the conversation context small with handoff notes and /clear at task boundaries. Use when a ClaudeCtx hook message reports the context size or finds a handoff, when a task is done and the next request is unrelated, when the user says devam or continue after /clear, or asks about token use, context size, handoff or skill cost."
---

# ClaudeCtx: one task per session, state in a handoff note

Every request re-sends the whole conversation, so cost grows with session length. `/clear` costs nothing; `/compact` is itself a large request. Keep each session to one task and carry state across `/clear` in a short handoff note.

## 1. Handoff + /clear (main rule)
Do this when a ClaudeCtx hook reports the soft or hard limit, or when a task is done and the user's next request is unrelated:
1. Never mid-edit. Finish the current step and leave the code consistent first.
2. Write the handoff note to the path the hook gave. Without a hook message, get it with `python "${CLAUDE_SKILL_DIR}/scripts/ctx_hook.py" path`. At most 40 lines, in this template:
   ```
   # Handoff YYYY-MM-DD HH:MM
   Goal: <one line>
   Status: <done, or what remains>
   Done: <changed files, one line each>
   User rules: <the user's constraints, verbatim>
   Decisions: <choices made and why, one line each>
   Open issues: <bugs, unverified assumptions>
   Next step: <the very next action>
   Key paths/commands: <build/test commands, files with line refs>
   ```
   No code dumps and no history narrative. Keep only what the next session cannot rediscover cheaply.
3. Tell the user in one line: the context size, the handoff path, and "run /clear, then say devam". You cannot run /clear yourself. `/rename` before `/clear` keeps the old session findable.
4. Do not nag. If the user wants to stay in this context, continue; offer again only when the hook reports a higher level.

## 2. After /clear
When the SessionStart hook points to a handoff and the user continues ("devam", "continue"), read the note first and trust it. Re-read only the files the next step touches. If the user starts something else, ignore the note. When its task is finished, overwrite the note with the new state or delete it.

## 3. While working (slows context growth)
- Read large files by range (offset/limit) or with Grep. Do not re-read files that have not changed.
- Cap command output: tail/head, quiet flags, failures-only test output.
- Verbose jobs (full test runs, log analysis, broad searches): propose a subagent, so only its summary enters this context. Its own tokens still count.
- An unrelated question in a large session: suggest a new session instead of answering here.

## 4. Measuring and skill cost
- `python "${CLAUDE_SKILL_DIR}/scripts/ctx_report.py" --since YYYY-MM-DD [--clear-at 120000]`: context per request, sessions, idle cache misses, and a what-if estimate.
- `python "${CLAUDE_SKILL_DIR}/scripts/skill_audit.py"`: listing cost and usage of each skill, with a suggested `skillOverrides` block.

Report results as measured X -> Y and never promise percentages. Change settings.json (hooks, `skillOverrides`, `autoCompactWindow`) only after showing a diff and getting the user's explicit approval.
