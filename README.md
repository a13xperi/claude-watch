# claude-watch

Real-time terminal dashboard for monitoring Claude Code token usage, session activity, and burn rate.

Built on Textual for Claude Code Max subscribers who run multiple concurrent sessions and need visibility into their 5-hour and 7-day rate limit windows.

## What it shows

- **Token Monitor** -- 5h and 7d window usage bars with countdown/reset times, account indicator (A/B/C), pacing prediction
- **Burndown Chart** -- ASCII chart of 5h window usage over time with rate markers, projection line, budget per 10min
- **Urgent Alerts** -- red banner for rate limit warnings (>80% usage, high burn rate)
- **Active Sessions** -- interactive table of running Claude CLI processes with parent/sub-row layout, live status, token delta, directive, and click-to-focus Warp terminal
- **Call History** -- aggregated tool calls per session, grouped by date, with call count, top tools, last tool, 5h% delta
- **Session History** -- all past sessions from transcript index, grouped by day, with model, duration, 5h%, output tokens, gravity center directive; drill down into accomplishments
- **Tool Frequency** -- most-used tools across all sessions
- **Skills Panel** -- skill/slash command usage stats
- **Agent Spawns** -- agent subprocesses spawned in last 7 days
- **Passive Drain** -- background token consumption with anomaly detection (Normal / Check / Spike)
- **System Health** -- all Claude processes + infrastructure with memory, start time, model, source, status

## Requirements

- Python 3.9+
- `textual` (pip install textual) -- TUI framework (includes `rich`)
- Claude Code CLI (`claude`) installed and running
- The PreToolUse hook (`token-tracker.sh`) logging to `~/.claude/logs/token-ledger.jsonl`

## Setup

### 1. Install

```bash
pip install textual
```

### 2. Hook setup

Copy `hooks/token-tracker.sh` to `~/.claude/hooks/` and register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/token-tracker.sh"
          }
        ]
      }
    ]
  }
}
```

### 3. Run

```bash
python3 claude_watch_tui.py
```

Or symlink it:

```bash
ln -s $(pwd)/claude_watch_tui.py ~/bin/claude-watch
chmod +x claude_watch_tui.py
```

## How it works

```
Claude Code session
    |
    v
PreToolUse hook fires --> token-tracker.sh
    |                         |
    |                    writes to:
    |                    /tmp/claude-token-ledger-{PID}.jsonl  (per-session)
    |                    ~/.claude/logs/token-ledger.jsonl     (global)
    |
    v
claude-watch reads:
    - token-ledger.jsonl              (tool calls, drain events)
    - /tmp/statusline-debug.json      (current rate limits)
    - /tmp/claude-directive-{PID}     (session directives)
    - /tmp/claude-token-state-{PID}   (per-session token state)
    - ~/.claude/projects/*/           (transcript .jsonl files for history)
    - ~/.claude/logs/session-index.jsonl (built/cached session index)
```

### Architecture

| File | Role |
|---|---|
| `claude_watch_tui.py` | Main entry point -- Textual TUI app |
| `claude_watch_data.py` | Data layer -- parsing, indexing, aggregation |
| `claude_watch_tui.tcss` | Textual CSS styles |
| `claude_watch.py` | Legacy Rich-only version (still works, superseded by TUI) |

### Session Index

On first run, claude-watch builds a session index at `~/.claude/logs/session-index.jsonl` by scanning all transcript files. Subsequent runs only parse new/modified files. This makes the all-time session history panel fast even with hundreds of transcripts.

### Hot Reload

A file watcher monitors `*.py` files in the project directory. When a source file changes, the TUI auto-restarts (exit code 42 restart loop).

## Panels

### Token Monitor (header)
5h and 7d rate limit bars with:
- Current usage percentage and pacing prediction
- 5h countdown to reset, 7d reset day
- Active account indicator (A/B/C)

### Burndown Chart
ASCII chart tracking 5h window usage over time:
- Rate markers and projection line
- Budget per 10-minute interval

### Urgent Alerts
Red banner triggered by:
- Usage exceeding 80%
- High burn rate relative to remaining window

### Active Sessions (interactive DataTable)
Live processes detected via `ps`. Two-level row layout:
- **Parent row:** start time, session ID (cc-PID), launch source, company, project, model, duration, token delta%, directive
- **Sub-row:** live state (`>>` tool active, `thinking...`, `~` recent, idle), elapsed since last call, token count, CPU%
- Press `Enter` or `f` to focus the session's Warp terminal window via AppleScript

### Call History
Aggregated tool calls per session, grouped by date:
- Call count, top tools used, last tool invoked
- 5h% delta consumed

### Session History
All past sessions from transcript files, grouped by day (Today / Yesterday / date):
- End time, duration, model (opus/sonnet/haiku)
- Estimated 5h% consumed, output tokens
- Gravity center directive
- Press `Enter` to drill down

### Session Drill-Down
Structured view of a session's accomplishments:
- Git commits, files edited/created
- Skills used, MCP operations
- Notable commands, user prompts
- Press `t` to toggle token breakdown

### Tool Frequency
Most-used tools across all sessions, ranked by invocation count.

### Skills Panel
Skill/slash command usage statistics.

### Agent Spawns
Agent subprocesses spawned in the last 7 days with count and last seen date.

### Passive Drain
Token burn between tool calls (background consumption):
- Status: Normal (green) / Check (yellow) / Spike (red)
- Per-event: delta%, burn rate, active session count

### System Health
All Claude-related processes plus infrastructure (node, python, etc.):
- Memory usage, start time, model, source, status

## Keybindings

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Refresh |
| `u` | Usage Metrics screen (7-day breakdown, daily sparkline, per-day table) |
| `m` | MCP Stats screen (server usage and top actions over 7 days) |
| `h` | Toggle System Health panel |
| `Tab` / `Shift+Tab` | Focus between panels |
| `/` | Search/filter sessions |
| `Escape` | Clear search |
| `Enter` / `f` | Focus Warp terminal (Active Sessions) or drill down (Session History) |
| `t` | Toggle token breakdown (in drill-down view) |

## CLI

```bash
# Launch the TUI
python3 claude_watch_tui.py

# Resume context packet for a specific session
python3 claude_watch_tui.py --session <PID> --context

# List recent sessions (table/JSON)
python3 claude_watch_tui.py --list
```

## Roadmap

- [ ] Nested row expansion in Call History and Session History
- [ ] Export session history to CSV
- [ ] Per-session cost estimation
- [ ] Alert notifications (system notification on spike)

## License

MIT
