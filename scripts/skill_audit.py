#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeCtx skill audit: what your personal and project skills cost in context (read-only).

Per the Claude Code skills docs: every skill in the listing adds its name and description
(description + when_to_use, capped at 1,536 chars) to the context on every turn, whether or not
it is used; a skill's body enters the context only when it is invoked and then stays there.
`skillOverrides` in settings can list a skill as "name-only", "user-invocable-only" or "off".

This script reports listing cost, body size and how often each skill was invoked in local
transcripts, and PRINTS a suggested `skillOverrides` block. It never edits settings or skills.
Token figures are chars/4 (rough, not a tokenizer). The built-in `/skill-doctor` gives the
official numbers; this script is for when you or Claude cannot run it.

  python skill_audit.py [--days 30] [--cwd DIR] [--json]
"""
import argparse
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import home_dir, long_path  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CAP = 1536


def frontmatter(text):
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not m:
        return {}, text
    fm, key = {}, None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if km and not line.startswith((" ", "\t")):
            key = km.group(1)
            fm[key] = km.group(2).strip()
        elif key:
            fm[key] = (fm[key] + " " + line.strip()).strip()
    for k, v in fm.items():
        v = re.sub(r"^[>|][+-]?\s*", "", v)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        fm[k] = v
    return fm, text[m.end():]


def overrides(home, cwd):
    out = {}
    for p in (os.path.join(home, ".claude", "settings.json"),
              os.path.join(cwd, ".claude", "settings.json"),
              os.path.join(cwd, ".claude", "settings.local.json")):
        try:
            with open(p, encoding="utf-8") as f:
                out.update((json.load(f) or {}).get("skillOverrides") or {})
        except (OSError, ValueError, AttributeError):
            pass
    return out


def usage_counts(projects_root, since_ts):
    counts = {}
    pat = re.compile(r"<command-name>/?([\w:-]+)</command-name>")
    for p in glob.glob(os.path.join(projects_root, "*", "*.jsonl")):
        try:
            if os.path.getmtime(long_path(p)) < since_ts:
                continue
            f = open(long_path(p), encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"Skill"' in line:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    for b in (d.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                            s = str((b.get("input") or {}).get("skill", "")).split(":")[-1]
                            counts[s] = counts.get(s, 0) + 1
                for s in pat.findall(line):
                    s = s.split(":")[-1]
                    counts[s] = counts.get(s, 0) + 1
    return counts


def startup_overhead(projects_root, scan=20):
    """What the newest session received before its first request, from transcript attachments.

    Observed in local transcripts, not a documented format: attachments such as `skill_listing`
    (with `isInitial`), `deferred_tools_delta`, `agent_listing_delta`, `mcp_instructions_delta`.
    Covers plugin and bundled skills too, which the SKILL.md scan above cannot see.
    """
    files = sorted(glob.glob(os.path.join(projects_root, "*", "*.jsonl")),
                   key=lambda p: os.path.getmtime(long_path(p)), reverse=True)
    for p in files[:scan]:
        items, listing = {}, None
        try:
            f = open(long_path(p), encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "assistant":
                    break
                a = d.get("attachment")
                if isinstance(a, dict) and a.get("type"):
                    c = a.get("content")
                    size = len(c) if isinstance(c, str) and c else len(json.dumps(a, ensure_ascii=False))
                    items[a["type"]] = items.get(a["type"], 0) + size
                    if a["type"] == "skill_listing" and a.get("isInitial") and isinstance(c, str):
                        listing = c
        if listing is None:
            continue
        groups = {}
        for e in re.split(r"\n(?=- )", listing):
            if not e.strip():
                continue
            name = e[2:].split(": ")[0] if e.startswith("- ") else e[:40]
            g = name.split(":")[0] if ":" in name else "(personal/bundled)"
            n, c = groups.get(g, (0, 0))
            groups[g] = (n + 1, c + len(e))
        return {"transcript": os.path.basename(p), "attachments_chars": dict(sorted(items.items(), key=lambda kv: -kv[1])),
                "listing_groups": {g: {"skills": n, "chars": c} for g, (n, c) in sorted(groups.items(), key=lambda kv: -kv[1][1])}}
    return None


def audit(home, cwd, days):
    ov = overrides(home, cwd)
    since_ts = time.time() - days * 86400
    used = usage_counts(os.path.join(home, ".claude", "projects"), since_ts)
    rows = []
    for scope, root in (("personal", os.path.join(home, ".claude", "skills")), ("project", os.path.join(cwd, ".claude", "skills"))):
        for p in sorted(glob.glob(os.path.join(root, "*", "SKILL.md"))):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            fm, body = frontmatter(text)
            name = fm.get("name") or os.path.basename(os.path.dirname(p))
            desc = (fm.get("description", "") + " " + fm.get("when_to_use", "")).strip()
            if not fm.get("description"):
                desc = next((l.strip() for l in body.splitlines() if l.strip()), "")
            state = ov.get(name, "on")
            if fm.get("disable-model-invocation", "").lower() == "true":
                state = "user-invocable-only" if state == "on" else state
            listed = 0 if state in ("off", "user-invocable-only") else len(name) + (min(len(desc), CAP) if state == "on" else 0)
            new = os.path.getmtime(p) > since_ts  # installed or edited inside the window: too new to judge
            rows.append({"skill": name, "scope": scope, "state": state, "listing_chars": listed,
                         "body_chars": len(body), "invocations": used.get(name, 0), "new": new})
    rows.sort(key=lambda r: -r["listing_chars"])
    suggest = {r["skill"]: "name-only" for r in rows
               if r["state"] == "on" and r["invocations"] == 0 and r["listing_chars"] > 150 and not r["new"]}
    return {"days": days, "skills": rows, "listing_chars_total": sum(r["listing_chars"] for r in rows),
            "suggested_skillOverrides": suggest,
            "startup": startup_overhead(os.path.join(home, ".claude", "projects"))}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30, help="usage window (transcripts modified in the last N days)")
    p.add_argument("--cwd", default=os.getcwd(), help="project folder for project skills and settings")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    out = audit(home_dir(), a.cwd, a.days)
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    print(f"{'skill':28} {'state':20} {'listing':>8} {'body':>7} {'used/' + str(a.days) + 'd':>8}")
    for r in out["skills"]:
        print(f"{r['skill'][:28]:28} {r['state']:20} {r['listing_chars']:8} {r['body_chars']:7} {r['invocations']:8}"
              + ("  new" if r["new"] else ""))
    t = out["listing_chars_total"]
    print(f"Listing total: {t:,} chars (~{t // 4:,} tokens) on every request. Body: only after invocation, then it stays.")
    if out["suggested_skillOverrides"]:
        print("Unused in this window with a description over 150 chars. Suggested (NOT applied; needs your approval):")
        print(json.dumps({"skillOverrides": out["suggested_skillOverrides"]}, indent=2))
    st = out["startup"]
    if st:
        print(f"\nStartup context of the newest session ({st['transcript'][:12]}), observed attachment sizes in chars:")
        for k, v in list(st["attachments_chars"].items())[:10]:
            print(f"  {v:7,}  {k}")
        print("Skill listing by source (plugin skills ignore skillOverrides; manage them with /plugin):")
        for g, v in st["listing_groups"].items():
            print(f"  {v['chars']:7,} chars ~{v['chars'] // 4:5,} tok  {v['skills']:3} skills  {g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
