# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeCtx shared helpers: config, transcript reading, handoff paths. Stdlib only."""
import hashlib
import json
import os
import re
from datetime import datetime

B = chr(92)  # backslash, kept out of literals so the file reads the same everywhere

DEFAULTS = {
    "soft": 120000,              # first warning: hand off at the next task boundary
    "hard": 200000,              # strong warning: finish the current step, then hand off
    "step": 50000,               # warn again every `step` tokens above `hard`
    "idle_minutes": 60,          # gap after which the prompt cache has likely expired
    "idle_min_context": 60000,   # ignore idle gaps for small contexts
    "handoff_max_age_days": 7,   # SessionStart mentions only handoffs newer than this
}


def home_dir(home=None):
    return home or os.environ.get("CLAUDECTX_HOME") or os.path.expanduser("~")


def long_path(path):
    """Windows MAX_PATH workaround: Claude Code project dirs can exceed 260 chars."""
    ap = os.path.abspath(path)
    if os.name != "nt" or len(ap) < 250 or ap.startswith(B + B):
        return ap
    return B + B + "?" + B + ap


def load_config(home=None):
    cfg = dict(DEFAULTS)
    p = os.path.join(home_dir(home), ".claude", "claudectx.json")
    try:
        with open(p, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    if not isinstance(user, dict):
        return cfg
    for k, v in user.items():
        if k in DEFAULTS and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            cfg[k] = v
    if user.get("lang") in ("en", "tr"):
        cfg["lang"] = user["lang"]
    return cfg


def context_of(usage):
    """Tokens the request carried: uncached input + cache reads + cache writes."""
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def parse_ts(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def project_key(cwd):
    key = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd or "."))
    if len(key) > 100:
        key = key[-80:].lstrip("-") + "-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return key


def handoff_path(cwd, home=None):
    return os.path.normpath(os.path.join(home_dir(home), ".claude", "handoffs", project_key(cwd) + ".md"))


def last_context(transcript_path, max_bytes=8 * 1024 * 1024):
    """Context size of the latest main-conversation request, read from the end of the transcript.

    Returns (tokens, unix_ts) or (None, None). The latest assistant usage gives the context of
    the last request plus its output; a later compact_boundary gives the post-compaction size.
    """
    try:
        f = open(long_path(transcript_path), "rb")
    except (OSError, TypeError, ValueError):
        return None, None
    with f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = 256 * 1024
        while True:
            start = max(0, size - chunk)
            f.seek(start)
            data = f.read(size - start)
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]  # first line may be cut
            for raw in reversed(lines):
                if not raw.strip():
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("isSidechain"):
                    continue
                if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                    post = (d.get("compactMetadata") or {}).get("postTokens")
                    if isinstance(post, int):
                        return post, parse_ts(d.get("timestamp"))
                if d.get("type") == "assistant":
                    u = (d.get("message") or {}).get("usage")
                    if u:
                        return context_of(u) + (u.get("output_tokens") or 0), parse_ts(d.get("timestamp"))
            if start == 0 or chunk >= max_bytes:
                return None, None
            chunk *= 4


def iter_requests(projects_root):
    """Yield one dict per main-conversation API request in every transcript (deduped by message id)."""
    import glob
    for p in sorted(glob.glob(os.path.join(projects_root, "*", "*.jsonl"))):
        seen = set()
        try:
            f = open(long_path(p), encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line and "compact_boundary" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("isSidechain"):
                    continue
                if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                    yield {"file": p, "session": d.get("sessionId"), "ts": d.get("timestamp", ""), "compact": True,
                           "ctx": (d.get("compactMetadata") or {}).get("postTokens") or 0}
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                if d.get("type") != "assistant" or not u:
                    continue
                mid = m.get("id") or d.get("uuid")
                if mid in seen or context_of(u) == 0:  # zero usage: synthetic/error entries, not real requests
                    continue
                seen.add(mid)
                yield {"file": p, "session": d.get("sessionId"), "ts": d.get("timestamp", ""), "compact": False,
                       "ctx": context_of(u), "out": u.get("output_tokens") or 0, "model": m.get("model", "")}
