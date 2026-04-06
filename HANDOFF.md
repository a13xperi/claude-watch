# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.4 — merged panels + aligned columns)

**Changes since v0.3.1:**
- Active Calls merged INTO Active Sessions as inline sub-rows (state, ago, tok, cpu)
- All panels share consistent first 4 columns: When, Session, Src, Mdl
- Model shows context tier (opus:1m, sonnet:1m)
- Green dot (●) for active sessions in Call History and Session History
- RUNAWAY/HIGH BURN alerts are actionable — name top burner PID, suggest `kill PID` if idle >5m
- PID mapping (transcript UUID → cc-PID) for Session History
- Last Tool column added to Call History
- Mdl column added to Call History
- Sub-row labels (ago:, tok:, cpu:) for readability
- Removed separate Active Calls panel and ToolCallFeed widget
- Single-pass ledger scan in sessions panel (perf improvement)

**Layout order (top to bottom):**
1. Token Monitor header (5h/7d bars, pacing, account)
2. Urgent Alerts (token expiry, runaway burn — actionable with kill PID)
3. Active Sessions with inline sub-rows (state, ago, tokens, cpu per session)
4. Call History (all sessions from ledger, with model + last tool)
5. Session History (indexed transcripts, PID-mapped, green dot for active)
6. Passive Drain
7. Tool Frequency + Skills (side by side)

**Two versions:**
- `claude-watch` → Textual TUI (symlink: `~/bin/claude-watch`)
- `claude-watch-rich` → Rich Live fallback (may be behind on some panels)
- Shared data layer: `claude_watch_data.py`

**Hook:** `~/.claude/hooks/token-tracker.sh` — captures tool_snippet, model, output_tokens

**Keybindings:**
- `q` quit, `r` refresh, `u` usage metrics, `Tab`/`Shift+Tab` focus panels
- `Enter` on Session History → drill-down, `Escape` back
- Mouse wheel / arrow keys to scroll full dashboard

## What's Next (v0.5)

### Priority: Click-to-focus session terminal
Alex wants to click (or press a key on) an active session and have it bring that terminal window to the front. Sessions run in Warp.

**Implementation approach (confirmed feasible):**
- `ps -p {PID} -o tty=` returns the TTY (e.g., ttys015)
- Parent process chain leads to `/Applications/Warp.app/...`
- Use AppleScript via `osascript` to activate the Warp window/tab containing that TTY
- Trigger: could be pressing 1/2/3 for session index, or converting Active Sessions to a DataTable for row selection
- Need to investigate Warp's AppleScript/accessibility support for tab focusing

### Other v0.5 ideas
- Hot reload (no restart needed after code changes)
- Usage metrics: per-day breakdown, trend sparkline
- Rich version parity
- Agent tracking panel (which subagent types spawned, how often)
- MCP call tracking (tools starting with `mcp__`)
- Active Sessions panel still uses old column order on first load before restart — consider if the old `make_sessions_panel` is cached somewhere

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
# Mouse wheel or arrow keys to scroll past Call History to see Session History, Drain, etc.
```
