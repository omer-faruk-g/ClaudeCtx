#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeCtx hook: tells Claude (and you) how big the conversation context is.

Claude cannot see its own context size. This hook reads it from the transcript `usage`
fields and speaks only when it matters, so below the soft limit it costs 0 tokens.

  ctx_hook.py prompt   UserPromptSubmit: warn once per level (soft, hard, then every `step`),
                       and note a likely prompt-cache miss after an idle gap
  ctx_hook.py start    SessionStart (startup|clear): point Claude to a fresh handoff note
  ctx_hook.py path     print the handoff path for the current folder (or --cwd DIR)

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
from _common import handoff_path, home_dir, last_context, load_config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MSG = {
    "en": {
        "soft_user": "ClaudeCtx: context ~{k}K tokens. Claude will write a handoff at the next task boundary; then run /clear.",
        "hard_user": "ClaudeCtx: context ~{k}K tokens. Every message re-sends all of it. Finish this step, then hand off and /clear.",
        "idle_user": "ClaudeCtx: idle {m} min, prompt cache likely expired: this message re-sent ~{k}K tokens uncached.",
        "start_user": "ClaudeCtx: handoff from {when} found. Say 'continue' to resume from it.",
    },
    "tr": {
        "soft_user": "ClaudeCtx: bağlam ~{k}K token. Claude bir sonraki görev sınırında devir notu yazacak; sonra /clear yaz.",
        "hard_user": "ClaudeCtx: bağlam ~{k}K token. Her mesaj bunun tamamını yeniden gönderiyor. Bu adımı bitir, devir notu + /clear.",
        "idle_user": "ClaudeCtx: {m} dk ara, önbellek büyük olasılıkla soğudu: bu mesaj ~{k}K tokeni önbelleksiz gönderdi.",
        "start_user": "ClaudeCtx: {when} tarihli devir notu var. Kaldığın yerden sürmek için 'devam' de.",
    },
}

CLAUDE_SOFT = ("ClaudeCtx: context is ~{k}K tokens (soft limit {soft}K). Keep working, but at the next task "
               "boundary (never mid-edit) invoke the claudectx skill: write the handoff to {path} and ask the "
               "user to run /clear. If the user's new message starts an unrelated task, do this before starting it.")
CLAUDE_HARD = ("ClaudeCtx: context is ~{k}K tokens (hard limit {hard}K); every request re-sends all of it. Finish "
               "only the current step, then invoke the claudectx skill, write the handoff to {path} and ask the "
               "user to run /clear. Do not start new subtasks in this context.")
CLAUDE_IDLE = ("ClaudeCtx: the last reply was {m} min ago, so the prompt cache has likely expired and this request "
               "re-sent ~{k}K tokens uncached. If the user's message starts a new task, suggest a handoff and /clear first.")
CLAUDE_START = ("ClaudeCtx: a handoff note from {when} exists for this folder: {path} (goal: {goal}). If the user "
                "continues that work (e.g. 'devam', 'continue'), read it first and do not re-explore what it already "
                "records. Otherwise ignore it.")


def state_file(session_id, home=None):
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or "nosession"))[:80] or "nosession"
    return os.path.join(home_dir(home), ".claude", "claudectx-state", sid + ".json")


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
        if level >= cfg["hard"]:
            claude.append(CLAUDE_HARD.format(k=k, hard=cfg["hard"] // 1000, path=path))
            user.append(lang["hard_user"].format(k=k))
        else:
            claude.append(CLAUDE_SOFT.format(k=k, soft=cfg["soft"] // 1000, path=path))
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
    goal = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip().lstrip("#-* ").strip()
                if s.lower().startswith(("goal", "hedef")):
                    goal = s.split(":", 1)[-1].strip()[:160]
                    break
    except OSError:
        return None, None
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    lang = MSG.get(cfg.get("lang", "en"), MSG["en"])
    return (CLAUDE_START.format(when=when, path=path, goal=goal or "see file"),
            lang["start_user"].format(when=when))


def emit(event_name, context, message):
    out = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}
    if message:
        out["systemMessage"] = message
    print(json.dumps(out, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["prompt", "start", "path"])
    p.add_argument("--cwd", help="folder for `path` (default: current directory)")
    a = p.parse_args(argv)
    cfg = load_config()
    if a.mode == "path":
        print(handoff_path(a.cwd or os.getcwd()))
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
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
