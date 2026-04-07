# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.7 — merged history, MCP stats, agents panel, hot reload)

**v0.7 features (built on top of v0.6):**
- **Hot reload**: File watcher auto-restarts TUI when .py files change (exit code 42 restart loop)
- **ActiveSessionsTable interactive**: Converted from Static to interactive DataTable with Enter/`f` to focus Warp terminal. Sub-rows show live state (ago, tok, cpu)
- **Merged Call History into Session History**: Call History panel removed. Tool call data (call count + tool breakdown) now shows as expandable sub-rows in Session History
- **Co (Company) column**: Added across System Health, Session History, and the merged history view. Derived from project name
- **Mdl column in System Health**: Shows the model each session is running
- **Memory-sorted System Health**: Highest memory consumer sorts to top
- **AgentsPanel**: New panel showing agent spawn stats over 7 days (type, count, last seen)
- **MCPStatsScreen**: Press `m` for MCP tool usage breakdown by server (7-day window)
- **DailySparklinePanel**: 7-day output token sparkline in Usage Metrics
- **Project column alignment**: System Health columns aligned with Active Sessions widths

**Previous (v0.5 / v0.6):**
- v0.6: Click-to-focus (AppleScript/AXRaise), usage sparkline (`u`), gravity center quality, dynamic burndown chart, System Health enriched (start, source, model, company)
- v0.5: Gravity center directives, project column, accomplishments drill-down, token view toggle (`t`), CCID persistence, CLI lookup, session list, TUI search (`/`)

**Layout order (top to bottom):**
1. Token Monitor header (5h/7d bars, pacing, account)
2. Search bar (hidden, press `/` to show)
3. Urgent Alerts (token expiry, runaway burn — actionable with kill PID)
4. Active Sessions — interactive DataTable with inline sub-rows (state, ago, tokens, cpu) + Project + Co columns
5. Session History — merged with Call History; indexed transcripts + tool call sub-rows (call count, tool breakdown), PID-mapped, green dot, project, company, gravity center
6. Passive Drain
7. Tool Frequency + Skills + Agent Spawns (side by side)

**Two versions:**
- `claude-watch` → Textual TUI (symlink: `~/bin/claude-watch`)
- `claude-watch-rich` → Rich Live fallback (may be behind on some panels)
- Shared data layer: `claude_watch_data.py`

**Hook:** `~/.claude/hooks/token-tracker.sh` — captures tool_snippet, model, output_tokens

**Keybindings:**
- `q` quit, `r` refresh, `u` usage metrics, `m` MCP stats, `h` toggle health, `Tab`/`Shift+Tab` focus panels
- `/` search/filter sessions, `Escape` clear search
- `Enter`/`f` on Active Sessions → focus that session's Warp terminal
- `Enter` on Session History → drill-down (accomplishments view)
- `t` in drill-down → toggle token breakdown
- `m` → MCP tool usage breakdown by server
- Mouse wheel / arrow keys to scroll full dashboard

**CLI:**
- `python3 claude_watch_tui.py --session 72887` → JSON lookup by CCID
- `python3 claude_watch_tui.py --session 72887 --context` → resume context packet
- `python3 claude_watch_tui.py --list` → recent sessions table/JSON

## What's Next (v0.8)

### Ideas
- Export session history to CSV
- Per-session cost estimation ($)
- Alert notifications (system notification on spike)
- Nested row expansion in Session History (expand to see full tool call detail)
- Multi-account capacity view (A/B/C usage side by side)

## Key Context
- Python 3.9.6 (no `X | None` type hints)
- Textual 8.2.3
- 1% of 5h window ~ 5,500 output tokens
- paperclip_agents.json maps 25 agents across KAA/Delphi/SAGE/Personal/Adinkra
- Session index rebuilds in background; delete ~/.claude/logs/session-index.jsonl to force full rebuild

## How To Verify
```bash
cd ~/projects/claude-watch

# TUI dashboard
python3 claude_watch_tui.py
# press 'u' for usage metrics
# press '/' to search sessions
# Tab to Session History, Enter on a row for accomplishments drill-down
# press 't' in drill-down for token view

# CLI lookup
python3 claude_watch_tui.py --session 72887
python3 claude_watch_tui.py --session 72887 --context
python3 claude_watch_tui.py --list
```
