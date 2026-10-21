# ClaudeCtx

ClaudeCtx is a [Claude Code](https://code.claude.com) skill that keeps the conversation context small. Claude Code sends the whole conversation with every request, so a session that runs for days costs more on every message, even a one-line question. Claude cannot see how big its own context is. ClaudeCtx gives it that number. At task boundaries Claude condenses the whole session into a short core-context note and clears the session itself; right after the clear, the note is loaded back, so nothing important is lost. It also shows what your skills and plugins cost on every request.

## What it does

| Part | What happens |
|---|---|
| Context meter (`UserPromptSubmit` hook) | Reads the size of the last request from the transcript's `usage` fields. Below the soft limit it prints nothing, so it costs 0 tokens. At 120K it tells Claude to hand off when the current task is done; at 200K, to finish the current step and hand off; then again every 50K. Each level is reported once per session. You see a one-line notice too. |
| Idle-gap note | If your last reply was 60+ minutes ago and the context is 60K+, the prompt cache has likely expired and the message was re-sent uncached. Claude suggests a fresh session if you are starting something new. |
| Core context | At a task boundary (never mid-edit) Claude runs `ctx_hook.py digest`, which prints every message you typed in the session, including the parts compaction already dropped, so your rules survive verbatim. It then writes a note of at most 40 lines (goal, status, changed files, your rules, decisions, open issues, next step, key commands) to `~/.claude/handoffs/<folder>.md`. |
| Automatic clear | In the Claude desktop app, Claude then clears its own session with the app's `clear_session` tool (`session_id: "self"`); the app asks you to approve, and the old conversation stays available under "Resume previous session". In the terminal or an IDE, Claude asks you to run `/clear`. Set `"auto_clear": false` to always be asked. |
| Reload (`SessionStart` hook, `startup\|clear`) | Right after `/clear`, a note written in the last 30 minutes is loaded in full: the new session starts with the core context and nothing else. An older note (up to 7 days) gets a one-line pointer; say "devam" or "continue" and Claude reads it instead of re-exploring. |
| Working rules | Read big files by range, cap command output, hand verbose jobs to a subagent, keep one task per session. |
| `ctx_report.py` | Context per request (median, p90, max), sessions with their span and idle cache misses, and a what-if: the input-side total if a handoff + `/clear` had happened at a given size. |
| `skill_audit.py` | What each skill adds to the listing that is sent on every request, how often it was used, and a suggested `skillOverrides` block. It also lists what the newest session received at startup, including plugin skills grouped by plugin. It only prints; it changes nothing. |

The skill itself is small: a 460-character listing entry, and a 3.8K-character body that loads only when a hook message or your request needs it.

## Install

```bash
git clone https://github.com/omer-faruk-g/ClaudeCtx ~/.claude/skills/claudectx
```

Then add the two hooks to `~/.claude/settings.json`. Merge them with any hooks you already have. If `python` is not on your PATH, use `python3` or the interpreter's full path; if `~` is not expanded on your system, write the script's full path.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python ~/.claude/skills/claudectx/scripts/ctx_hook.py prompt", "timeout": 5 } ] }
    ],
    "SessionStart": [
      { "matcher": "startup|clear", "hooks": [ { "type": "command", "command": "python ~/.claude/skills/claudectx/scripts/ctx_hook.py start", "timeout": 5 } ] }
    ]
  }
}
```

Optional settings in `~/.claude/claudectx.json` (defaults shown; `lang` is `en` or `tr` for the notices you see; `autoload_minutes` is how fresh a note must be to load in full after `/clear`):

```json
{ "soft": 120000, "hard": 200000, "step": 50000, "idle_minutes": 60, "idle_min_context": 60000, "handoff_max_age_days": 7, "autoload_minutes": 30, "auto_clear": true, "lang": "en" }
```

Measure before and after:

```bash
python ~/.claude/skills/claudectx/scripts/ctx_report.py --since 2026-10-01 --clear-at 120000
```

```bash
python ~/.claude/skills/claudectx/scripts/skill_audit.py
```

Tests: `python ~/.claude/skills/claudectx/tests/test_claudectx.py` (34 tests, stdlib only).

## Example (one user's transcripts, 880 requests)

`autoCompactWindow: 300000` was already set. Even so:

- The median request carried 173K tokens and the largest 274K. One session ran 755 requests over five days.
- The what-if for a handoff + `/clear` at 120K came to 20 % fewer input-side tokens; at 200K, 13 %. This is an upper bound: it assumes the handoff note carried everything the next session needed.
- Savings level off because every fresh session already starts at about 66K tokens (system prompt, tools, skill listing, hooks).
- The skill listing alone was about 30K characters (~7.5K tokens): 48 personal and bundled skills, 17 skills from one plugin, 22 from another. Plugin skills are managed with `/plugin`, not `skillOverrides`.

Run `ctx_report.py` and `skill_audit.py` on your own transcripts; your numbers will differ.

## Limits

- Claude can clear its own session only in the Claude desktop app, through the app's `clear_session` tool, and the app asks you to approve each clear (auto mode may decide without asking). In the terminal and IDEs you type `/clear`.
- The digest relies on the transcript's `isMeta` and `isCompactSummary` fields to skip injected text. They are observed, not documented.
- A handoff note drops detail. After `/clear`, Claude may have to re-read some files, so real savings are below the what-if.
- The meter reads the transcript, which can lag the conversation by a turn. Treat the number as approximate.
- `skill_audit.py`'s startup section reads transcript attachments (`skill_listing`, `deferred_tools_delta`, …). That format is observed, not documented, and may change.
- On Windows the hook adds about 0.3 s per prompt, mostly Python start-up.
- 120K and 200K are reasonable defaults, not a measured optimum.
- ClaudeCtx does not shrink the fixed startup context. `/context` and `skill_audit.py` show what fills it; turning off unused plugins and skills is up to you.
- Nothing is changed in your settings without you. The hooks are added by hand, and `skillOverrides` suggestions are only printed.

## Background

From the Claude Code docs:

- [Manage costs effectively](https://code.claude.com/docs/en/costs): token cost grows with context size; clear between unrelated tasks; `/clear` costs nothing, while `/compact` reads the whole conversation and is itself a large request; a long-open session draws usage for its whole history on every message; the first message after the cache lifetime reprocesses the full context.
- [Skills](https://code.claude.com/docs/en/skills): every listed skill adds its name and description to the context on every turn; a skill's body stays in context once loaded; `skillOverrides` can set a skill to `name-only`, `user-invocable-only` or `off`; `/skill-doctor` reports per-skill cost.
- [Hooks](https://code.claude.com/docs/en/hooks): `UserPromptSubmit` and `SessionStart` can add `additionalContext` for Claude; `SessionStart` reports whether it follows `startup`, `resume`, `clear`, `compact` or `fork`; hooks receive `transcript_path` and `cwd`.

## Türkçe özet

ClaudeCtx, Claude Code'da bağlamı küçük tutan bir skill. Claude Code her istekte konuşmanın tamamını yeniden gönderir; günlerce süren bir oturumda tek satırlık bir soru bile tüm geçmişin maliyetini taşır. Claude kendi bağlamının ne kadar büyük olduğunu göremez. ClaudeCtx:

- Bir hook ile bağlam boyutunu Claude'a ve sana bildirir. 120K altında hiçbir şey yazmaz, yani maliyeti 0'dır. 120K'da "görev sınırında devret", 200K'da "bu adımı bitir ve devret" der.
- Görev sınırında Claude oturumda yazdığın tüm mesajları okur (sıkıştırmanın attığı kısımlar dahil), en fazla 40 satırlık bir öz bağlam notu yazar ve oturumu kendisi temizler. Bunu masaüstü uygulamasında `clear_session` aracıyla yapar; uygulama her temizlik için onayını ister. Terminalde `/clear`'ı sen yazarsın.
- `/clear`'dan hemen sonra not yeni oturuma tamamen yüklenir: yeni oturum yalnızca öz bağlamla başlar. Daha eski bir not için yalnızca hatırlatma çıkar; "devam" dersen Claude notu okur.
- 60 dakikadan uzun aradan sonra önbelleğin büyük olasılıkla soğuduğunu söyler.
- `ctx_report.py` gerçek kullanımını ölçer ve "şu eşikte temizleseydin ne olurdu" tahmini verir.
- `skill_audit.py` her skill'in ve eklentinin her istekte ne kadar yer kapladığını gösterir. Ayarlarına hiçbir şey yazmaz.

Kurulum için yukarıdaki `git clone` komutunu çalıştır ve iki hook'u `~/.claude/settings.json`'a ekle.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
