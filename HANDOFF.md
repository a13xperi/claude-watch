# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.3.1 — layout + data polish)

**Changes since v0.3:**
- Dashboard is fully scrollable (ScrollableContainer)
- Session History moved below Call History (own row)
- Tool Frequency + Skills moved to bottom
- Tool Call Feed removed (redundant with Last Tool Activity)
- All times show seconds (HH:MM:SS everywhere)
- All durations show seconds (XmYYs format)
- Active Sessions: Start/Dur before PID, cc- prefix on PIDs, model column
- Last Tool Activity: Time (HH:MM:SS) as leftmost column
- Call History: "— Today —" separator not truncated
- Renamed "Active Calls" → "Last Tool Activity" (it shows history, not live calls)
- Fixed UnboundLocalError crash in bar() when token data is '?'
- Fixed "unnamed session" bug (hook no longer creates directive file preemptively)
- Ledger cache always loads full file (no stale slices from different last_n)
- Hook now captures `model` and `output_tokens` per tool call

**Layout order (top to bottom):**
1. Token Monitor header
2. Active Sessions (Start, Dur, PID, Mdl, Used, Status, Source, Directive)
3. Last Tool Activity (per-session grouped, last 3 tools each)
4. Call History (all sessions from ledger, aggregated tool counts)
5. Session History (indexed transcripts, per-session output tokens)
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

## What's Next (v0.4)

### Priority: Real "Active Calls" panel
The renamed "Last Tool Activity" shows historical tool calls — not live activity.
The real feature Alex wants: see in-flight prompts as they happen, like the statusline
shows `Boondoggling... (2m 2s · ↓ 1.5k tokens)`.

**Requirements:**
- Show each session's current state: idle, thinking, tool-calling
- Show elapsed time of current turn
- Show tokens consumed in current turn
- Real-time updates (not just tool-call snapshots)

**Data sources:**
- `/tmp/statusline-debug.json` has model, output_tokens, context usage — but is shared (last session to render)
- Per-session transcripts at `~/.claude/projects/.../SESSION_ID.jsonl` have per-turn data
- Hook now logs `model` and `output_tokens` — helps for historical, not live
- May need a new hook (turn_start / turn_end) or transcript polling

**Approach options:**
1. Poll each active session's transcript file for the latest assistant turn
2. Add a PrePromptSubmit hook that writes turn-start timestamp
3. Watch process CPU — if a claude PID is consuming CPU, it's actively generating

### Other v0.4 ideas
- Merge Last Tool Activity into Call History (they're redundant — Call History has better data, Last Tool Activity has better per-session grouping)
- Hot reload (no restart needed after code changes)
- Usage metrics: per-day breakdown, trend sparkline
- Rich version parity
- Agent tracking panel (which subagent types spawned, how often)
- MCP call tracking (tools starting with `mcp__`)

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
