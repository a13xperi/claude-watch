# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.5 — gravity center + project + accomplishments + CCID)

**Changes since v0.4:**
- **Gravity center directives**: Completed sessions show what was accomplished (first commit message, files edited, skills used) instead of "unnamed session"
- **Project column**: New column in all tables (Session History, Call History, Active Sessions) showing which project a session worked on (atlas, claude-watch, openclaw, etc.)
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
5. Call History (all sessions from ledger, with model + last tool + project)
6. Session History (indexed transcripts, PID-mapped, green dot, project, gravity center)
7. Passive Drain
8. Tool Frequency + Skills (side by side)

**Two versions:**
- `claude-watch` → Textual TUI (symlink: `~/bin/claude-watch`)
- `claude-watch-rich` → Rich Live fallback (may be behind on some panels)
- Shared data layer: `claude_watch_data.py`

**Hook:** `~/.claude/hooks/token-tracker.sh` — captures tool_snippet, model, output_tokens

**Keybindings:**
- `q` quit, `r` refresh, `u` usage metrics, `Tab`/`Shift+Tab` focus panels
- `/` search/filter sessions, `Escape` clear search
- `Enter` on Session History → drill-down (accomplishments view)
- `t` in drill-down → toggle token breakdown
- Mouse wheel / arrow keys to scroll full dashboard

**CLI:**
- `python3 claude_watch_tui.py --session 72887` → JSON lookup by CCID
- `python3 claude_watch_tui.py --session 72887 --context` → resume context packet
- `python3 claude_watch_tui.py --list` → recent sessions table/JSON

## What's Next (v0.6)

### Click-to-focus session terminal
Alex wants to click (or press a key on) an active session and have it bring that terminal window to the front. Sessions run in Warp.

**Implementation approach (from v0.5 handoff):**
- `ps -p {PID} -o tty=` returns the TTY (e.g., ttys015)
- Parent process chain leads to `/Applications/Warp.app/...`
- Use AppleScript via `osascript` to activate the Warp window/tab containing that TTY
- Trigger: convert Active Sessions to DataTable for row selection, or use 1/2/3 keys
- Need to investigate Warp's AppleScript/accessibility support for tab focusing

### Other v0.6 ideas
- Hot reload (no restart needed after code changes)
- Usage metrics: per-day breakdown, trend sparkline
- Rich version parity with new features
- Agent tracking panel (which subagent types spawned, how often)
- MCP call tracking (tools starting with `mcp__`)
- Gravity center quality: handle more commit message formats (squash commits, conventional commits)

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
