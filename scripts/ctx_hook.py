#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeCtx hook: tells Claude (and you) how big the conversation context is.

Claude cannot see its own context size. This hook reads it from the transcript `usage`
fields and speaks only when it matters, so below the soft limit it costs 0 tokens.

  ctx_hook.py prompt   UserPromptSubmit: warn once per level (soft, hard, then every `step`),
                       and note a likely prompt-cache miss after an idle gap
  ctx_hook.py start    SessionStart (startup|clear): in the first session after a handoff is written, load it
                       in full (the core context); otherwise point Claude to a recent one
  ctx_hook.py path     print the handoff path for the current folder (or --cwd DIR)
  ctx_hook.py digest   print the user's own messages from a transcript (--transcript, default: newest),
                       so the handoff can be built from the whole session, not only what compaction kept

Config (optional): ~/.claude/claudectx.json, e.g. {"soft": 120000, "hard": 200000, "lang": "tr"}.
A hook must never block your prompt: on any error it writes to stderr and exits 0 silently.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (handoff_path, home_dir, last_context, load_config, newest_transcript,  # noqa: E402
                     user_messages)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MSG = {
    "en": {
        "soft_user": "ClaudeCtx: context ~{k}K tokens. When this task is done, Claude will write the core context and clear the session.",
        "hard_user": "ClaudeCtx: context ~{k}K tokens. Every message re-sends all of it. After this step: core context + /clear.",
        "idle_user": "ClaudeCtx: idle {m} min, prompt cache likely expired: this message re-sent ~{k}K tokens uncached.",
        "start_user": "ClaudeCtx: handoff from {when} found. Say 'continue' to resume from it.",
        "load_user": "ClaudeCtx: core context loaded (handoff from {when}).",
    },
    "tr": {
        "soft_user": "ClaudeCtx: bağlam ~{k}K token. Bu görev bitince Claude öz bağlamı yazıp oturumu temizleyecek.",
        "hard_user": "ClaudeCtx: bağlam ~{k}K token. Her mesaj bunun tamamını yeniden gönderiyor. Bu adımdan sonra öz bağlam + /clear.",
        "idle_user": "ClaudeCtx: {m} dk ara, önbellek büyük olasılıkla soğudu: bu mesaj ~{k}K tokeni önbelleksiz gönderdi.",
        "start_user": "ClaudeCtx: {when} tarihli devir notu var. Kaldığın yerden sürmek için 'devam' de.",
        "load_user": "ClaudeCtx: öz bağlam yüklendi ({when} tarihli devir notu).",
    },
}

CLAUDE_SOFT = ("ClaudeCtx: context is ~{k}K tokens (soft limit {soft}K). Keep working; when the current task is "
               "done (never mid-edit), invoke the claudectx skill and run its core-context flow: handoff {path}, "
               "transcript {transcript}. {clear}")
CLAUDE_HARD = ("ClaudeCtx: context is ~{k}K tokens (hard limit {hard}K); every request re-sends all of it. Finish "
               "only the current step, then invoke the claudectx skill and run its core-context flow: handoff "
               "{path}, transcript {transcript}. {clear} Do not start new subtasks in this context.")
CLEAR_SELF = ("Then clear the session yourself as the last action of that turn: clear_session tool with "
              "session_id \"self\" (Claude desktop app); without that tool, ask the user to run /clear.")
CLEAR_ASK = "Then ask the user to run /clear (auto_clear is off)."
CLAUDE_LOAD = ("ClaudeCtx core context: the handoff note written at {when}, just before the session was cleared ({path}). It "
               "replaces the cleared conversation. Treat it as the current state, follow its User rules, and "
               "continue from its Next step when the user continues. Re-read only files the next step touches.\n\n{note}")
NOTE_CAP = 7000  # hook output is capped at 10,000 chars; a 40-line note fits easily
CLAUDE_IDLE = ("ClaudeCtx: the last reply was {m} min ago, so the prompt cache has likely expired and this request "
               "re-sent ~{k}K tokens uncached. If the user's message starts a new task, suggest a handoff and /clear first.")
CLAUDE_START = ("ClaudeCtx: a handoff note from {when} exists for this folder: {path} (goal: {goal}). If the user "
                "continues that work (e.g. 'devam', 'continue'), read it first and do not re-explore what it already "
                "records. Otherwise ignore it.")


def state_file(session_id, home=None):
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or "nosession"))[:80] or "nosession"
    return os.path.join(home_dir(home), ".claude", "claudectx-state", sid + ".json")


def loaded_file(note_path, home=None):
    name = os.path.splitext(os.path.basename(note_path))[0]
    return os.path.join(home_dir(home), ".claude", "claudectx-state", "loaded-" + name + ".json")


def claim_autoload(note_path, mtime, home=None):
    """True the first time this version of the note is seen, so only one session loads it in full."""
    marker = loaded_file(note_path, home)
    try:
        with open(marker, encoding="utf-8") as f:
            if json.load(f).get("mtime") == mtime:
                return False
    except (OSError, ValueError, AttributeError):
        pass  # no marker yet, or a broken one: this version was not loaded
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"mtime": mtime}, f)
    except OSError as e:  # still load it; the next session may load it again
        print(f"claudectx start: cannot write {marker}: {e}", file=sys.stderr)
    return True


def level_for(tokens, cfg):
    if tokens >= cfg["hard"]:
        step = max(cfg["step"], 1)
        return cfg["hard"] + step * ((tokens - cfg["hard"]) // step)
    if tokens >= cfg["soft"]:
        return cfg["soft"]
    return 0


def prompt_event(data, cfg, home=None, now=None):
    """Return (additionalContext, systemMessage) or (None, None)."""
    tokens, ts = last_context(data.get("transcript_path"))
    if tokens is None:
        return None, None
    now = now or time.time()
    k = round(tokens / 1000)
    lang = MSG.get(cfg.get("lang", "en"), MSG["en"])
    path = handoff_path(data.get("cwd") or os.getcwd(), home)
    claude, user = [], []

    sf = state_file(data.get("session_id"), home)
    try:
        with open(sf, encoding="utf-8") as f:
            warned = int(json.load(f).get("level", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        warned = 0
    level = level_for(tokens, cfg)
    if (level == 0 and warned != 0) or level > warned:  # reset after compaction/clear, or record a new level
        os.makedirs(os.path.dirname(sf), exist_ok=True)
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({"level": level, "tokens": tokens, "at": int(now)}, f)
    if level > warned:
        where = dict(k=k, path=path, transcript=data.get("transcript_path") or "(newest)",
                     clear=CLEAR_SELF if cfg.get("auto_clear", True) else CLEAR_ASK)
        if level >= cfg["hard"]:
            claude.append(CLAUDE_HARD.format(hard=cfg["hard"] // 1000, **where))
            user.append(lang["hard_user"].format(k=k))
        else:
            claude.append(CLAUDE_SOFT.format(soft=cfg["soft"] // 1000, **where))
            user.append(lang["soft_user"].format(k=k))

    if ts and tokens >= cfg["idle_min_context"]:
        idle = int((now - ts) // 60)
        if idle >= cfg["idle_minutes"]:
            claude.append(CLAUDE_IDLE.format(m=idle, k=k))
            user.append(lang["idle_user"].format(m=idle, k=k))

    if not claude:
        return None, None
    return " ".join(claude), " ".join(user)


def prune_state(home=None, now=None, max_age_days=30):
    d = os.path.join(home_dir(home), ".claude", "claudectx-state")
    cutoff = (now or time.time()) - max_age_days * 86400
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        p = os.path.join(d, n)
        try:
            if n.endswith(".json") and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass  # another session may be using it; try again next start


def start_event(data, cfg, home=None, now=None):
    prune_state(home, now)
    path = handoff_path(data.get("cwd") or os.getcwd(), home)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    now = now or time.time()
    if now - mtime > cfg["handoff_max_age_days"] * 86400:
        return None, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            note = f.read()
    except OSError:
        return None, None
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    lang = MSG.get(cfg.get("lang", "en"), MSG["en"])
    # /clear sends "clear"; the desktop app's clear_session opens a new session, which sends "startup".
    if (data.get("source") in ("startup", "clear") and now - mtime <= cfg["autoload_minutes"] * 60
            and claim_autoload(path, mtime, home)):
        if len(note) > NOTE_CAP:
            note = note[:NOTE_CAP] + f"\n[... cut at {NOTE_CAP} chars; read the rest from the file]"
        return (CLAUDE_LOAD.format(when=when, path=path, note=note.strip()),
                lang["load_user"].format(when=when))
    goal = ""
    for line in note.splitlines():
        s = line.strip().lstrip("#-* ").strip()
        if s.lower().startswith(("goal", "hedef")):
            goal = s.split(":", 1)[-1].strip()[:160]
            break
    return (CLAUDE_START.format(when=when, path=path, goal=goal or "see file"),
            lang["start_user"].format(when=when))


def digest(transcript_path, per_msg=240, cap=10000):
    """The user's messages, one line each; past `cap` chars the middle is dropped (old rules + recent asks stay)."""
    lines, last = [], None
    for ts, text in user_messages(transcript_path):
        if text == last:
            continue
        last = text
        when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"
        lines.append(f"- [{when}] " + (text if len(text) <= per_msg else text[:per_msg] + " …"))
    if sum(len(x) + 1 for x in lines) <= cap:
        return lines
    head, tail, used = [], [], 0
    budget = cap // 2
    for x in lines:
        if used + len(x) + 1 > budget:
            break
        head.append(x)
        used += len(x) + 1
    used = 0
    for x in reversed(lines[len(head):]):
        if used + len(x) + 1 > budget:
            break
        tail.append(x)
        used += len(x) + 1
    tail.reverse()
    return head + [f"- [... {len(lines) - len(head) - len(tail)} messages omitted ...]"] + tail


def emit(event_name, context, message):
    out = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}
    if message:
        out["systemMessage"] = message
    print(json.dumps(out, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["prompt", "start", "path", "digest"])
    p.add_argument("--cwd", help="folder for `path` (default: current directory)")
    p.add_argument("--transcript", help="transcript for `digest` (default: the newest one)")
    a = p.parse_args(argv)
    cfg = load_config()
    if a.mode == "path":
        print(handoff_path(a.cwd or os.getcwd()))
        return 0
    if a.mode == "digest":
        tp = a.transcript or newest_transcript(os.path.join(home_dir(), ".claude", "projects"))
        lines = digest(tp)
        print(f"User messages in {tp} ({len(lines)} lines):")
        print("\n".join(lines) if lines else "(none found)")
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
        if a.mode == "prompt":
            ctx, msg = prompt_event(data, cfg)
            event = "UserPromptSubmit"
        else:
            if data.get("source") not in (None, "startup", "clear"):
                return 0
            ctx, msg = start_event(data, cfg)
            event = "SessionStart"
        if ctx:
            emit(event, ctx, msg)
    except Exception as e:  # a meter must never block the prompt; report and step aside
        print(f"claudectx {a.mode}: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
