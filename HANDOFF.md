# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.3 — partially shipped)

**Shipped this session:**
- Account awareness in header (A/B/C + name + lane)
- Tool input capture in hook (80-char snippet of commands/file paths/skill names)
- Token pacing prediction in header ("100% in ~18m at 0.6%/min")
- Active Calls shows tool snippets ("Bash: python3 ..." not just "Bash")
- Negative delta fix ("reset" instead of "-90%" after 5h window reset)
- Who column in Session History (source/directive instead of UUID)
- 5h% column in Active Calls
- 911 sessions indexed across all project dirs

**Two versions:**
- `claude-watch` → Textual TUI (symlink: `~/bin/claude-watch` → `~/projects/claude-watch/claude_watch_tui.py`)
- `claude-watch-rich` → Rich Live fallback
- Shared data layer: `claude_watch_data.py`
- Also symlinked: `~/bin/claude_watch_data.py`, `~/bin/claude_watch_tui.tcss`

**Hook:** `~/.claude/hooks/token-tracker.sh` — now captures `tool_snippet` field

## What To Build Next (v0.3 remaining)

Full plan: `~/.claude/plans/temporal-stargazing-raven.md`

### Remaining features (in priority order):

**4. Skills frequency panel** — new panel under Tool Frequency showing which Claude Code skills (/claim-task, /paperclip, etc.) are called, how often, when. Data from ledger where tool=="Skill" + tool_snippet has skill name.

**5. Call History panel** — scrollable DataTable showing ALL historical sessions with aggregated tool calls. Columns: Session | Source | When | Calls | Tools Used | 5h% Used | Directive. Purpose: see which sessions consumed what.

**6. Session drill-down** — Enter on a session in Session History → new Screen with per-turn token breakdown. Needs `_get_session_turns(session_id)` function + `SessionDrillDown(Screen)` in Textual. Need to store `project_dir` in session index.

**7. Granular Paperclip source** — parse project_id + agent_id from directory path, mapping file `paperclip_agents.json`. Display "KAA/DevOps" not just "paperclip".

**8. Usage metrics dashboard** — aggregate tokens by source/company/agent, show % of 7d budget.

### Bugs to fix:
- Idle label: show `· 19m` not `● idle`
- Session index duplicates (append-only JSONL, no dedup on rebuild)
- Textual header may need `max-height: 7` in CSS (was 5, now has 4 rows)

## Key Context
- Python 3.9.6 (no `X | None` type hints)
- Textual 8.2.3
- DataTable `add_column(width=N)` for fixed widths
- All project dirs scanned under `~/.claude/projects/`
- 1% of 5h window ≈ 5,500 output tokens
- Tool snippets only appear on NEW tool calls (old ledger entries lack the field)

## How To Verify
```bash
cd ~/projects/claude-watch
python3 claude_watch_tui.py    # Textual version
python3 claude_watch.py         # Rich version
python3 -c "from claude_watch_data import _get_session_history; print(len(_get_session_history()), 'sessions')"
```
