# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.3 — COMPLETE)

**All features shipped:**
- Account awareness (A/B/C + name + lane in header)
- Token pacing prediction ("100% in ~23m at 4.0%/min")
- Tool input capture with snippets ("Read: /Us..." → full path visible)
- Skills frequency panel (which `/skill` commands called, how often)
- Call History table (all sessions from ledger, aggregated tool counts)
- Session History table (indexed transcripts, per-session output tokens)
- Session drill-down (Enter on Session History → per-turn token breakdown)
- Usage metrics screen (press `u` → tokens by source, bar chart, 7d context)
- Granular Paperclip source: `KAA/CEO`, `Delphi/Writer` etc. (25 agents mapped)
- Layout: Call History + Session History side-by-side (2fr), feed-row below (1fr)
- All heuristic source detection replaced with index-backed lookups

**Two versions:**
- `claude-watch` → Textual TUI (symlink: `~/bin/claude-watch`)
- `claude-watch-rich` → Rich Live fallback
- Shared data layer: `claude_watch_data.py`

**Hook:** `~/.claude/hooks/token-tracker.sh` — captures `tool_snippet` field

**Keybindings:**
- `q` quit, `r` refresh, `u` usage metrics, `Tab`/`Shift+Tab` focus panels
- `Enter` on Session History → drill-down, `Escape` back

## What's Next (v0.4 ideas)

- Hot reload (no restart needed after code changes)
- Usage metrics: per-day breakdown, trend sparkline
- Paperclip: auto-refresh agent mapping from live API on startup
- Rich version parity for new panels
- Session drill-down from Call History table (currently only Session History)

## Key Context
- Python 3.9.6 (no `X | None` type hints)
- Textual 8.2.3
- 1% of 5h window ≈ 5,500 output tokens
- paperclip_agents.json maps 25 agents across KAA/Delphi/SAGE/Personal/Adinkra
- Session index rebuilds in background; delete ~/.claude/logs/session-index.jsonl to force full rebuild

## How To Verify
```bash
cd ~/projects/claude-watch
python3 claude_watch_tui.py    # main dashboard
# press 'u' for usage metrics
# Tab to focus a table, arrows to scroll, Enter on session history row to drill down
python3 -c "from claude_watch_data import _get_usage_metrics, _load_index; _load_index(); m,t=_get_usage_metrics(); print(f'{t/1000:.0f}k tokens, {len(m)} sources')"
```
