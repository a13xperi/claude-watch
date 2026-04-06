# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.3 — nearly complete)

**Shipped previously:**
- Account awareness in header (A/B/C + name + lane)
- Tool input capture in hook (80-char snippet of commands/file paths/skill names)
- Token pacing prediction in header ("100% in ~18m at 0.6%/min")
- Active Calls shows tool snippets ("Bash: python3 ..." not just "Bash")
- Negative delta fix ("reset" instead of "-90%" after 5h window reset)
- Who column in Session History (source/directive instead of UUID)
- 5h% column in Active Calls
- 911 sessions indexed across all project dirs

**Shipped this session:**
- Skills frequency panel (under Tool Frequency, shows /skill calls + count + last used)
- Call History panel (scrollable DataTable, all sessions from ledger with aggregated tools)
- Session drill-down (Enter on Session History → per-turn token breakdown screen)
- Bug fix: idle label shows `· 19m` not `● idle`
- Bug fix: session index dedup (rewrite instead of append)
- Bug fix: header max-height 7 (was 5, now fits pacing row)
- `project_dir` stored in session index for transcript lookup

**Two versions:**
- `claude-watch` → Textual TUI (symlink: `~/bin/claude-watch` → `~/projects/claude-watch/claude_watch_tui.py`)
- `claude-watch-rich` → Rich Live fallback
- Shared data layer: `claude_watch_data.py`
- Also symlinked: `~/bin/claude_watch_data.py`, `~/bin/claude_watch_tui.tcss`

**Hook:** `~/.claude/hooks/token-tracker.sh` — captures `tool_snippet` field

## What To Build Next (v0.3 remaining)

Full plan: `~/.claude/plans/temporal-stargazing-raven.md`

### Remaining features:

**7. Granular Paperclip source** — parse project_id + agent_id from directory path, mapping file `paperclip_agents.json`. Display "KAA/DevOps" not just "paperclip".

**8. Usage metrics dashboard** — aggregate tokens by source/company/agent, show % of 7d budget.

### Nice-to-haves:
- Drill-down from Call History table (currently only Session History supports Enter)
- Rich version parity for new panels (skills, call history)
- Hot reload without restart

## Key Context
- Python 3.9.6 (no `X | None` type hints)
- Textual 8.2.3
- DataTable `add_column(width=N)` for fixed widths
- All project dirs scanned under `~/.claude/projects/`
- 1% of 5h window ≈ 5,500 output tokens
- Tool snippets only appear on NEW tool calls (old ledger entries lack the field)
- Session drill-down parses transcripts directly via `_get_session_turns(session_id)`

## How To Verify
```bash
cd ~/projects/claude-watch
python3 claude_watch_tui.py    # Textual version
python3 claude_watch.py         # Rich version
python3 -c "from claude_watch_data import _get_session_history; print(len(_get_session_history()), 'sessions')"
python3 -c "from claude_watch_data import _get_call_history; h=_get_call_history(); print(f'{len(h)} sessions in call history')"
python3 -c "from claude_watch_data import _get_session_turns, _load_index; _load_index(); turns=_get_session_turns(list(__import__('claude_watch_data')._index_cache.keys())[0]); print(f'{len(turns)} turns')"
```
