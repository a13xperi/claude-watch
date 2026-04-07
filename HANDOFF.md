# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.6 — fully featured)

**v0.6 features (built on top of v0.5):**
- **Click-to-focus**: Active Sessions converted from Static panel to interactive DataTable. Enter or `f` key on any active session brings its Warp terminal window to front via AppleScript/AXRaise. Matches by conversation title or directive text.
- **Usage sparkline**: Press `u` for 7-day daily sparkline + per-day token breakdown table
- **MCP Stats screen**: Press `m` for MCP server usage and top actions over 7 days
- **Agent Spawns panel**: Shows subagent types spawned in last 7 days with count and last seen
- **Hot reload**: File watcher auto-restarts TUI when .py files change (exit code 42 restart loop)
- **Gravity center quality**: Normalizes commit messages (strips conventional prefixes, filters generic/merge commits)
- **Company column**: All tables show Co (company) derived from project
- **Dynamic burndown chart**: Width adapts to terminal, stats moved below chart
- **System Health enriched**: Start time, source, model, company columns added

**v0.5 features:**
- **Gravity center directives**: Completed sessions show what was accomplished (first commit message, files edited, skills used) instead of "unnamed session"
- **Project column**: New column in all tables showing which project a session worked on
- **Accomplishments drill-down**: Enter on a session shows structured view: git commits, files edited/created, skills, MCP ops, notable commands, user prompts
- **Token view toggle**: Press `t` in drill-down to switch between accomplishments and per-turn token breakdown
- **CCID persistence**: cc-PID → UUID mapping stored in session-index.jsonl, survives process death
- **CLI lookup**: `python3 claude_watch_tui.py --session 72887` for JSON output, `--context` for resume packet
- **Session list**: `python3 claude_watch_tui.py --list` shows recent sessions with projects
- **TUI search**: Press `/` to filter sessions by CCID, project, directive, or UUID

**Layout order (top to bottom):**
1. Token Monitor header (5h/7d bars, pacing, account)
2. Search bar (hidden, press `/` to show)
3. Urgent Alerts (token expiry, runaway burn — actionable with kill PID)
4. Active Sessions with inline sub-rows (state, ago, tokens, cpu per session) + Project column
5. Call History (all sessions from ledger, with model + last tool + project + company)
6. Session History (indexed transcripts, PID-mapped, green dot, project, company, gravity center)
7. Passive Drain
8. Tool Frequency + Skills + Agent Spawns (side by side)

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
- Mouse wheel / arrow keys to scroll full dashboard

**CLI:**
- `python3 claude_watch_tui.py --session 72887` → JSON lookup by CCID
- `python3 claude_watch_tui.py --session 72887 --context` → resume context packet
- `python3 claude_watch_tui.py --list` → recent sessions table/JSON

## What's Next (v0.7)

### Ideas
- Nested row expansion in Call History and Session History (same parent/sub-row pattern as Active Sessions)
- Export session history to CSV
- Per-session cost estimation
- Alert notifications (system notification on spike)

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
