#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeCtx report: where your context tokens go, from local transcripts (read-only).

  (1) context per main-conversation request: median / p90 / max, share above 100K / 200K
  (2) sessions: requests, time span, peak, input-side total, idle gaps that likely missed the cache
  (3) what-if: input-side tokens if a handoff + /clear had happened whenever context reached
      --clear-at. This is an ESTIMATE and an upper bound on savings: it assumes the handoff
      note carried everything needed, while real sessions re-read some files after /clear.

Token counts come from the transcript `usage` fields. Filters use each request's own
timestamp, so a long session is split correctly at --since/--until.

  python ctx_report.py --since 2026-10-11
  python ctx_report.py --since 2026-10-11 --clear-at 120000 --json
"""
import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import home_dir, load_config, parse_ts  # noqa: E402
from _common import iter_requests  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HANDOFF_TOKENS = 2000  # rough size of a handoff note read after /clear


def simulate(reqs, clear_at):
    """Input-side total if context had been reset to the session floor at `clear_at`."""
    if not reqs:
        return 0
    floor = next((r["ctx"] for r in reqs if not r["compact"] and r["ctx"] > 0), 0)
    total, sim, prev = 0, None, None
    for r in reqs:
        if r["compact"]:
            prev = r["ctx"]
            continue
        if sim is None:
            sim = r["ctx"]
        else:
            delta = r["ctx"] - prev if prev is not None and r["ctx"] >= prev else 0
            if sim >= clear_at:
                sim = floor + HANDOFF_TOKENS
            sim = min(r["ctx"], sim + delta)
        prev = r["ctx"]
        total += sim
    return total


def build(reqs, cfg, clear_at):
    main = [r for r in reqs if not r["compact"]]
    ctx = sorted(r["ctx"] for r in main)
    out = {"requests": len(main)}
    if ctx:
        tot = sum(ctx)
        out.update({
            "median": int(statistics.median(ctx)), "p90": ctx[min(len(ctx) - 1, int(len(ctx) * 0.9))], "max": ctx[-1],
            "input_side_total": tot,
            "share_over_100k": round(sum(c > 100000 for c in ctx) / len(ctx), 3),
            "share_over_200k": round(sum(c > 200000 for c in ctx) / len(ctx), 3),
        })
    sessions = {}
    for r in reqs:
        sessions.setdefault((r["file"], r["session"]), []).append(r)
    rows, sim_total = [], 0
    for (fp, sid), rs in sessions.items():
        rs.sort(key=lambda r: r["ts"])
        m = [r for r in rs if not r["compact"]]
        if not m:
            continue
        gaps, gap_tokens = 0, 0
        for a, b in zip(m, m[1:]):
            ta, tb = parse_ts(a["ts"]), parse_ts(b["ts"])
            if ta and tb and tb - ta >= cfg["idle_minutes"] * 60 and b["ctx"] >= cfg["idle_min_context"]:
                gaps += 1
                gap_tokens += b["ctx"]
        s = simulate(rs, clear_at)
        sim_total += s
        rows.append({"project": os.path.basename(os.path.dirname(fp))[-40:], "session": (sid or "?")[:8],
                     "requests": len(m), "first": m[0]["ts"][:16], "last": m[-1]["ts"][:16],
                     "peak": max(r["ctx"] for r in m), "input_side_total": sum(r["ctx"] for r in m),
                     "idle_cache_misses": gaps, "idle_miss_tokens": gap_tokens, "what_if_total": s})
    rows.sort(key=lambda r: -r["input_side_total"])
    out["sessions"] = rows
    out["what_if"] = {"clear_at": clear_at, "input_side_total": sim_total,
                      "note": "estimate / upper bound on savings; assumes the handoff note carried everything needed"}
    return out


def main(argv=None):
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", help="YYYY-MM-DD, by request timestamp (UTC)")
    p.add_argument("--until", help="YYYY-MM-DD, exclusive")
    p.add_argument("--project", help="substring of the project folder name")
    p.add_argument("--clear-at", type=int, default=cfg["soft"], help="what-if threshold (default: soft limit)")
    p.add_argument("--projects", default=os.path.join(home_dir(), ".claude", "projects"))
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    reqs = [r for r in iter_requests(a.projects)
            if (not a.since or r["ts"] >= a.since) and (not a.until or r["ts"] < a.until)
            and (not a.project or a.project.lower() in r["file"].lower())]
    out = build(reqs, cfg, a.clear_at)
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    if not out["requests"]:
        print("No requests in this window.")
        return 0
    print(f"=== (1) Context per request: {out['requests']} requests ===")
    print(f"  median {out['median']:,} | p90 {out['p90']:,} | max {out['max']:,} tokens")
    print(f"  >100K: {out['share_over_100k']:.0%} of requests | >200K: {out['share_over_200k']:.0%}")
    print(f"  input-side total: {out['input_side_total']:,} tokens (cache reads included; they count toward plan limits)")
    print(f"=== (2) Sessions (largest first, top {a.top}) ===")
    for r in out["sessions"][:a.top]:
        print(f"  {r['project']} {r['session']}: {r['requests']} req, {r['first']} -> {r['last']}, "
              f"peak {r['peak']:,}, total {r['input_side_total']:,}, idle cache misses {r['idle_cache_misses']} "
              f"(~{r['idle_miss_tokens']:,} tok re-sent)")
    w = out["what_if"]
    saved = out["input_side_total"] - w["input_side_total"]
    print(f"=== (3) What-if: handoff + /clear at {w['clear_at']:,} tokens ===")
    print(f"  {out['input_side_total']:,} -> {w['input_side_total']:,} input-side tokens "
          f"({saved / out['input_side_total']:.0%} fewer). {w['note']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
