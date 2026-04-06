# claude-watch

Real-time terminal dashboard for monitoring Claude Code token usage, session history, and burn rate.

Built for Claude Code Max subscribers who run multiple concurrent sessions and need visibility into their 5-hour and 7-day rate limit windows.

## What it shows

- **Token Monitor** -- 5h and 7d window usage bars with countdown/reset times
- **Active Sessions** -- running Claude CLI processes with PID, age, token delta, live status indicator
- **Session History** -- all past sessions grouped by day, with model, duration, output tokens, and estimated 5h% usage
- **Tool Call Feed** -- real-time log of every tool invocation with session directive and per-call token ticks
- **Passive Drain** -- token burn between tool calls with anomaly detection (Normal / Check / Spike)
- **Tool Frequency** -- breakdown of most-used tools across the session

## Requirements

- Python 3.9+
- `rich` (pip install rich)
- Claude Code CLI (`claude`) installed and running
- The PreToolUse hooks (`token-tracker.sh`) logging to `~/.claude/logs/token-ledger.jsonl`

## Setup

### 1. Install

```bash
pip install rich
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
python3 claude_watch.py
```

Or symlink it:

```bash
ln -s $(pwd)/claude_watch.py ~/bin/claude-watch
chmod +x claude_watch.py
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
    - token-ledger.jsonl          (tool calls, drain events)
    - /tmp/statusline-debug.json  (current rate limits)
    - /tmp/claude-directive-{PID} (session names)
    - ~/.claude/projects/*/       (transcript files for history)
```

### Session Index

On first run, claude-watch builds a session index at `~/.claude/logs/session-index.jsonl` by scanning all transcript files. Subsequent runs only parse new/modified files. This makes the all-time session history panel fast even with hundreds of transcripts.

## Panels

### Token Monitor (header)
Shows 5h and 7d rate limit bars with:
- Current usage percentage
- 5h countdown to reset
- 7d reset day

### Active Sessions
Live processes detected via `ps`. Shows:
- PID, age, cumulative token delta
- Status: `⚡ running` (tool call in last 45s), `↺ idle 2m30s`, `● idle`
- Session directive

### Session History
All past sessions from transcript files, grouped by day (Today / Yesterday / date). Shows:
- End time, duration, model used (opus/sonnet/haiku)
- Estimated 5h% consumed (today only, from ledger interpolation)
- Output tokens generated
- Last user prompt or auto-generated slug

### Tool Call Feed
Real-time feed of tool invocations from the ledger. Shows:
- Timestamp, session ID, directive, tool name
- Per-call token delta with tick detection (highlights the exact call that caused a % increase)

### Passive Drain
Token burn happening between tool calls (background consumption). Includes:
- Status line: Normal (green) / Check (yellow) / Spike (red)
- Per-event: delta %, burn rate, active session count, desktop app status

## Roadmap

- [ ] Keyboard navigation and scrollable panels (Textual migration)
- [ ] Subagent session filtering
- [ ] Export session history to CSV
- [ ] Per-session detailed view (drill into a past session)
- [ ] Alert notifications (terminal bell / system notification on spike)

## License

MIT
