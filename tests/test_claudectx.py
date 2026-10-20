# SPDX-License-Identifier: GPL-3.0-or-later
"""ClaudeCtx tests (stdlib unittest). Run: python tests/test_claudectx.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import _common  # noqa: E402
import ctx_hook  # noqa: E402
import ctx_report  # noqa: E402
import skill_audit  # noqa: E402

CFG = dict(_common.DEFAULTS)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def assistant(ctx, ts, out=100, sidechain=False, mid=None, session="s1"):
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": iso(ts), "sessionId": session,
            "message": {"id": mid or f"m{ctx}-{ts}", "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 2, "cache_read_input_tokens": ctx - 1002,
                                  "cache_creation_input_tokens": 1000, "output_tokens": out}}}


def compact(post, ts, session="s1"):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": iso(ts), "sessionId": session,
            "compactMetadata": {"trigger": "auto", "preTokens": 300000, "postTokens": post}}


class Base(unittest.TestCase):
    def setUp(self):
        # Under home, not %TEMP%: Store Python on Windows virtualizes AppData writes.
        self.home = tempfile.mkdtemp(prefix=".claudectx-test-", dir=os.path.expanduser("~"))
        self.cwd = os.path.join(self.home, "proj")
        os.makedirs(self.cwd)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def transcript(self, entries, name="t.jsonl"):
        p = os.path.join(self.home, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return p

    def data(self, tp, session="s1"):
        return {"transcript_path": tp, "session_id": session, "cwd": self.cwd}


class TestCommon(Base):
    def test_levels(self):
        self.assertEqual(ctx_hook.level_for(119999, CFG), 0)
        self.assertEqual(ctx_hook.level_for(120000, CFG), 120000)
        self.assertEqual(ctx_hook.level_for(199999, CFG), 120000)
        self.assertEqual(ctx_hook.level_for(200000, CFG), 200000)
        self.assertEqual(ctx_hook.level_for(260000, CFG), 250000)

    def test_last_context_uses_latest_main_request(self):
        now = time.time()
        tp = self.transcript([assistant(50000, now - 20), assistant(90000, now - 10),
                              assistant(400000, now - 5, sidechain=True), {"type": "user", "timestamp": iso(now)}])
        tokens, ts = _common.last_context(tp)
        self.assertEqual(tokens, 90000 + 100)
        self.assertAlmostEqual(ts, now - 10, delta=1)

    def test_last_context_after_compaction(self):
        now = time.time()
        tp = self.transcript([assistant(290000, now - 20), compact(19000, now - 10)])
        self.assertEqual(_common.last_context(tp)[0], 19000)

    def test_last_context_missing_or_bad(self):
        self.assertEqual(_common.last_context(os.path.join(self.home, "nope.jsonl")), (None, None))
        self.assertEqual(_common.last_context(None), (None, None))
        tp = self.transcript([])
        with open(tp, "w") as f:
            f.write("not json\n{broken\n")
        self.assertEqual(_common.last_context(tp), (None, None))

    def test_large_transcript_reads_from_end(self):
        now = time.time()
        filler = [{"type": "user", "timestamp": iso(now), "message": {"content": "x" * 5000}}] * 300
        tp = self.transcript([assistant(150000, now - 1)] + filler)
        self.assertEqual(_common.last_context(tp)[0], 150100)

    def test_project_key(self):
        short = _common.project_key("C:/Users/HP/Desktop/PhotonAI")
        self.assertNotIn(":", short)
        long_key = _common.project_key("C:/" + "a" * 300)
        self.assertLessEqual(len(long_key), 90)
        self.assertEqual(long_key, _common.project_key("C:/" + "a" * 300))
        self.assertNotEqual(long_key, _common.project_key("C:/" + "a" * 299 + "b"))

    def test_config(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        with open(os.path.join(self.home, ".claude", "claudectx.json"), "w") as f:
            json.dump({"soft": 90000, "hard": "x", "step": -5, "idle_minutes": True, "lang": "tr", "bogus": 1}, f)
        cfg = _common.load_config(self.home)
        self.assertEqual(cfg["soft"], 90000)
        self.assertEqual(cfg["hard"], 200000)
        self.assertEqual(cfg["step"], 50000)
        self.assertEqual(cfg["idle_minutes"], 60)
        self.assertEqual(cfg["lang"], "tr")
        self.assertNotIn("bogus", cfg)


class TestPromptHook(Base):
    def run_prompt(self, tokens, now=None, ts_offset=10, session="s1"):
        now = now or time.time()
        tp = self.transcript([assistant(tokens - 100, now - ts_offset, session=session)], name=f"{session}.jsonl")
        return ctx_hook.prompt_event(self.data(tp, session), CFG, home=self.home, now=now)

    def test_silent_below_soft(self):
        self.assertEqual(self.run_prompt(80000), (None, None))
        self.assertFalse(os.path.exists(ctx_hook.state_file("s1", self.home)))  # no write while quiet

    def test_prune_old_state(self):
        self.run_prompt(130000, session="old")
        self.run_prompt(130000, session="new")
        old = ctx_hook.state_file("old", self.home)
        t = time.time() - 40 * 86400
        os.utime(old, (t, t))
        ctx_hook.start_event({"cwd": self.cwd}, CFG, home=self.home)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(ctx_hook.state_file("new", self.home)))

    def test_soft_once_then_hard_then_steps(self):
        c, m = self.run_prompt(130000)
        self.assertIn("soft limit 120K", c)
        self.assertIn(_common.handoff_path(self.cwd, self.home), c)
        self.assertIn("/clear", m)
        self.assertEqual(self.run_prompt(150000), (None, None))  # same level: no repeat
        c, _ = self.run_prompt(210000)
        self.assertIn("hard limit 200K", c)
        self.assertEqual(self.run_prompt(240000), (None, None))
        self.assertIn("hard limit", self.run_prompt(255000)[0])

    def test_reset_after_compaction(self):
        self.assertIsNotNone(self.run_prompt(130000)[0])
        self.assertEqual(self.run_prompt(30000), (None, None))
        self.assertIsNotNone(self.run_prompt(125000)[0])

    def test_sessions_are_independent(self):
        self.assertIsNotNone(self.run_prompt(130000, session="a")[0])
        self.assertIsNotNone(self.run_prompt(130000, session="b")[0])

    def test_idle_gap(self):
        c, m = self.run_prompt(90000, ts_offset=90 * 60)
        self.assertIn("90 min ago", c)
        self.assertIn("90", m)
        self.assertEqual(self.run_prompt(40000, ts_offset=90 * 60), (None, None))

    def test_turkish_user_message(self):
        cfg = dict(CFG, lang="tr")
        tp = self.transcript([assistant(130000, time.time() - 5)])
        _, m = ctx_hook.prompt_event(self.data(tp), cfg, home=self.home)
        self.assertIn("bağlam", m)


class TestStartHook(Base):
    def write_handoff(self, age_days=0, text="# Handoff\nGoal: fix the login bug\nNext step: run tests\n"):
        p = _common.handoff_path(self.cwd, self.home)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
        return p

    def test_no_handoff(self):
        self.assertEqual(ctx_hook.start_event({"cwd": self.cwd}, CFG, home=self.home), (None, None))

    def test_fresh_handoff(self):
        p = self.write_handoff()
        c, m = ctx_hook.start_event({"cwd": self.cwd}, CFG, home=self.home)
        self.assertIn("fix the login bug", c)
        self.assertIn(p, c)
        self.assertIn("continue", m)

    def test_old_handoff_ignored(self):
        self.write_handoff(age_days=8)
        self.assertEqual(ctx_hook.start_event({"cwd": self.cwd}, CFG, home=self.home), (None, None))


class TestHookProcess(Base):
    def run_hook(self, mode, stdin):
        env = dict(os.environ, CLAUDECTX_HOME=self.home)
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, "ctx_hook.py"), mode], input=stdin,
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)

    def test_bad_input_never_blocks(self):
        r = self.run_hook("prompt", "{not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("claudectx prompt", r.stderr)

    def test_prompt_json_shape(self):
        tp = self.transcript([assistant(130000, time.time() - 5)])
        r = self.run_hook("prompt", json.dumps(self.data(tp)))
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("ClaudeCtx", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("systemMessage", out)

    def test_start_ignores_resume(self):
        TestStartHook.write_handoff(self)
        r = self.run_hook("start", json.dumps({"cwd": self.cwd, "source": "resume"}))
        self.assertEqual(r.stdout, "")
        r = self.run_hook("start", json.dumps({"cwd": self.cwd, "source": "clear"}))
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_path_mode(self):
        env = dict(os.environ, CLAUDECTX_HOME=self.home)
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "ctx_hook.py"), "path", "--cwd", self.cwd],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.stdout.strip(), _common.handoff_path(self.cwd, self.home))


class TestReport(Base):
    def projects(self):
        root = os.path.join(self.home, "projects")
        os.makedirs(os.path.join(root, "C--proj"))
        t0 = 1_800_000_000
        entries = [assistant(50000 + 20000 * i, t0 + 60 * i, mid=f"a{i}") for i in range(10)]
        entries.insert(3, dict(entries[2]))  # duplicate message id (parallel tool calls)
        entries.append(assistant(240000, t0 + 60 * 10 + 7200, mid="late"))  # after a 2-hour gap
        entries.append(compact(20000, t0 + 60 * 200))
        entries.append(assistant(25000, t0 + 60 * 201, mid="after"))
        with open(os.path.join(root, "C--proj", "s1.jsonl"), "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return root, t0

    def test_build(self):
        root, t0 = self.projects()
        reqs = list(_common.iter_requests(root))
        out = ctx_report.build(reqs, CFG, 120000)
        self.assertEqual(out["requests"], 12)  # duplicate dropped, compaction not a request
        s = out["sessions"][0]
        self.assertEqual(s["idle_cache_misses"], 1)
        self.assertEqual(s["peak"], 240000)
        self.assertLess(out["what_if"]["input_side_total"], out["input_side_total"])

    def test_simulation_never_exceeds_actual(self):
        reqs = [{"ctx": c, "compact": False} for c in (60000, 100000, 140000, 180000, 220000)]
        self.assertEqual(ctx_report.simulate(reqs, 10**9), sum(r["ctx"] for r in reqs))
        sim = ctx_report.simulate(reqs, 120000)
        self.assertLess(sim, sum(r["ctx"] for r in reqs))
        self.assertGreater(sim, 0)

    def test_since_filter_by_request_time(self):
        root, t0 = self.projects()
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "ctx_report.py"), "--projects", root, "--json",
                            "--since", iso(t0 + 60 * 5)[:19]], capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(r.stdout)["requests"], 7)


class TestAudit(Base):
    def skill(self, name, fm_extra="", desc="does things " * 20, age_days=60):
        d = os.path.join(self.home, ".claude", "skills", name)
        os.makedirs(d)
        p = os.path.join(d, "SKILL.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\ndescription: \"{desc}\"\n{fm_extra}---\n\n# {name}\nbody text\n")
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))

    def test_audit(self):
        self.skill("big-unused")
        self.skill("big-new", age_days=1)
        self.skill("big-used")
        self.skill("manual", "disable-model-invocation: true\n")
        self.skill("collapsed")
        self.skill("folded", desc="x")
        with open(os.path.join(self.home, ".claude", "skills", "folded", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: folded\ndescription: >\n  line one\n  line two\n---\nbody\n")
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            json.dump({"skillOverrides": {"collapsed": "name-only"}}, f)
        proj = os.path.join(self.home, ".claude", "projects", "C--p")
        os.makedirs(proj)
        with open(os.path.join(proj, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": "big-used"}}]}}) + "\n")
        out = skill_audit.audit(self.home, self.cwd, 30)
        rows = {r["skill"]: r for r in out["skills"]}
        self.assertEqual(rows["manual"]["listing_chars"], 0)
        self.assertEqual(rows["collapsed"]["listing_chars"], len("collapsed"))
        self.assertEqual(rows["big-used"]["invocations"], 1)
        self.assertGreater(rows["folded"]["listing_chars"], len("folded") + 15)
        self.assertTrue(rows["big-new"]["new"])
        self.assertIsNone(out["startup"])  # no transcript with a skill_listing attachment
        self.assertEqual(out["suggested_skillOverrides"], {"big-unused": "name-only"})

    def test_startup_overhead(self):
        proj = os.path.join(self.home, ".claude", "projects", "C--p")
        os.makedirs(proj)
        listing = "- tdd: write tests first\n- plug:one: plugin skill one\n- plug:two: plugin skill two"
        with open(os.path.join(proj, "s.jsonl"), "w", encoding="utf-8") as f:
            for e in ({"type": "attachment", "attachment": {"type": "skill_listing", "isInitial": True, "content": listing}},
                      {"type": "attachment", "attachment": {"type": "deferred_tools_delta", "content": "x" * 500}},
                      assistant(60000, time.time()),
                      {"type": "attachment", "attachment": {"type": "skill_listing", "content": "- late: after"}}):
                f.write(json.dumps(e) + "\n")
        st = skill_audit.startup_overhead(os.path.join(self.home, ".claude", "projects"))
        self.assertEqual(st["listing_groups"]["plug"]["skills"], 2)
        self.assertEqual(st["listing_groups"]["(personal/bundled)"]["skills"], 1)
        self.assertEqual(st["attachments_chars"]["deferred_tools_delta"], 500)
        self.assertEqual(st["attachments_chars"]["skill_listing"], len(listing))  # later delta not counted


if __name__ == "__main__":
    unittest.main(verbosity=1)
