# token-watch

Real-time terminal dashboard for monitoring Claude Code token usage, session activity, burn rate, and fleet operations across multiple accounts.

Built on Textual for Claude Code Max subscribers who run multiple concurrent sessions and need full visibility into rate limits, capacity, build output, and session coordination.

**Version: 0.16**

## What it shows

### Dashboard (home view)

- **Account Capacity Header** -- A/B/C capacity bars with 5h/7d usage, countdowns, and pacing
- **Burndown Chart** -- ASCII chart of 5h window usage over time with rate markers and projection line
- **Token Attribution** -- who (which session/account) is burning tokens
- **Urgent Alerts** -- red banner for rate limit warnings (>80% usage, high burn rate)
- **System Health** -- all Claude processes + infrastructure with memory, start time, model, source, status
- **Active Sessions** -- interactive table of running Claude CLI processes with parent/sub-row layout, live status, token delta, directive, and click-to-focus Warp terminal
- **Session Narrative** -- natural language summary of what the current session is doing
- **Session History** -- all past sessions from transcript index, grouped by day, with drill-down
- **Passive Drain** -- background token consumption with anomaly detection (Normal / Check / Spike)
- **Tool Frequency** -- most-used tools across all sessions
- **Skills Panel** -- skill/slash command usage statistics
- **Agents Panel** -- agent subprocesses spawned in the last 7 days

### Tab views

| Key | Tab | Description |
|---|---|---|
| `u` | Usage | 7-day breakdown, daily sparkline, per-day table, window scores |
| `m` | MCP | MCP server usage stats and top actions |
| `s` | Cycle | Current Pomodoro cycle task capture -- add/edit/roll/delete tasks per 5h window |
| `c` | Capacity | A/B/C account capacity side-by-side with 5h/7d bars |
| `y` | Cycles | All past Pomodoro cycles with scores and plan view |
| `x` | Test Queue | Build items with test status tracking |
| `l` | Leaderboard | Window scores and velocity rankings |
| `a` | Audit | Cross-cycle audit with drill-down |
| `M` | Mission Control | Everything shipped, grouped by company/project (from build_ledger) |
| `w` | Wire | Inter-session messages via Supabase session_messages |
| `g` | Rules | Hook/permission rules, events, block counts |
| `p` | Projects | Project task board |
| `v` | Advisor | AI advisor synthesis -- fleet health, capacity, suggestions |
| `i` | Inbox | Unified prioritized inbox (urgent / attention / fyi) |
| `t` | Analytics | Token utilization coaching -- fleet scorecard, account cards, heatmap, waste analysis, efficiency metrics, 10-rule suggestion engine |
| `h` | Health | Toggle System Health panel on dashboard |

### Screens (overlays)

- **SessionDrillDown** -- structured view of session accomplishments (commits, files, skills, tools)
- **TokenAccessScreen** -- token access control panel for Paperclip agents (toggle heartbeats)
- **CycleDetailScreen** -- single cycle deep dive
- **CyclePlanScreen** -- cycle planning view
- **TokenAttributionScreen** -- detailed token attribution breakdown
- **TestDetailScreen** -- test case detail view
- **BlockAssignScreen** -- assign tasks to Pomodoro blocks
- **HealthScreen** -- expanded health view
- **NavigationScreen** -- full navigation overlay

## Requirements

- Python 3.9+
- `textual` (pip install textual) -- TUI framework (includes `rich`)
- Claude Code CLI (`claude`) installed and running
- The PreToolUse hook (`token-tracker.sh`) logging to `~/.claude/logs/token-ledger.jsonl`
- Supabase project for build_ledger, session_locks, session_messages, account_capacity_history (optional but recommended)

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
token-watch
```

Or run directly:

```bash
python3 token_watch_tui.py
```

## Architecture

| File | Role |
|---|---|
| `token_watch_tui.py` | Main entry point -- Textual TUI app (~6800 lines) |
| `token_watch_data.py` | Data layer -- parsing, indexing, aggregation (~6600 lines) |
| `token_watch_advisor.py` | AI advisor engine + inbox synthesis |
| `token_watch_tui.tcss` | Textual CSS styles |
| `token_watch.py` | Legacy Rich-only version (superseded) |

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
PostToolUse hook --> logs commits to Supabase build_ledger
    |
    v
capacity-snapshot cron (every 5min) --> Supabase account_capacity_history
    |
    v
token-watch reads all sources:
    - token-ledger.jsonl              (tool calls, drain events)
    - /tmp/statusline-debug.json      (current rate limits)
    - /tmp/claude-directive-{PID}     (session directives)
    - /tmp/claude-token-state-{PID}   (per-session token state)
    - ~/.claude/projects/*/           (transcript .jsonl files for history)
    - ~/.claude/logs/session-index.jsonl   (built/cached session index)
    - ~/.claude/logs/window-scores.jsonl   (per-5h window scores)
    - Supabase: build_ledger, session_locks, session_messages, account_capacity_history
```

### Data sources

| Source | What | Location |
|---|---|---|
| Token Ledger | Per-tool-call: timestamps, burn rate, model, tokens | `~/.claude/logs/token-ledger.jsonl` |
| Session Index | Per-session: duration, tokens, model, directive | `~/.claude/logs/session-index.jsonl` |
| Window Scores | Per-5h-window: burn, parallelism, shipping, score | `~/.claude/logs/window-scores.jsonl` |
| Build Ledger | Per-commit: project, files, item type, points | Supabase `build_ledger` |
| Capacity History | Per-5min: account, 5h%, 7d%, reset timestamps | Supabase `account_capacity_history` |
| Session Locks | Active sessions: account, repo, task | Supabase `session_locks` |
| Session Messages | Inter-session wire messages | Supabase `session_messages` |

### Session Index

On first run, token-watch builds a session index at `~/.claude/logs/session-index.jsonl` by scanning all transcript files. Subsequent runs only parse new/modified files. This makes the all-time session history panel fast even with hundreds of transcripts.

### Hot Reload

A file watcher monitors `*.py` files in the project directory. When a source file changes, the TUI auto-restarts (exit code 42 restart loop).

### Auto-Refresh

All tabs auto-refresh when visible on throttled intervals (10--30s). No manual refresh needed, though `r` forces an immediate refresh of the current view.

### Lazy Loading

Tabs only load their data when first visited, keeping startup fast.

## Key features

### Tri-account management

Monitors 3 Claude Code Max accounts ($200/mo each) with per-account capacity tracking, 5h/7d reset countdowns, and rebalancing suggestions. The Capacity tab (`c`) shows all three side-by-side.

### Pomodoro cycle system

10x 30-minute blocks per 5h window. The Cycle tab (`s`) captures tasks, assigns them to blocks, tracks completion, and scores each cycle. The Cycles tab (`y`) shows history. Unfinished items roll forward automatically.

### Analytics / coaching engine

The Analytics tab (`t`) provides a fleet scorecard with utilization heatmaps, waste analysis, and efficiency metrics. A 10-rule suggestion engine generates prioritized coaching based on actual usage patterns. Switch time windows with `1` (24h), `2` (72h), `3` (1 week), `4` (1 month).

### Wire messaging

Sessions communicate via Supabase `session_messages` for file-lock coordination, status updates, and questions. The Wire tab (`w`) shows all messages. Messages auto-expire after 30 minutes.

### Mission Control

Everything shipped, grouped by company and project, pulled from the Supabase `build_ledger`. Each commit logged by the PostToolUse hook appears here with test status.

### Advisor

AI-synthesized fleet health analysis with capacity recommendations, inbox prioritization, and actionable suggestions. Available as a tab (`v`) or via CLI (`--advisor`).

### Token Access Control

Screen overlay for managing Paperclip agent heartbeats. Toggle individual agents on/off to control token consumption from automated processes.

## Keybindings

### Global

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Refresh current tab |
| `e` | Export session history to CSV |
| `/` | Search/filter sessions |
| `Tab` / `Shift+Tab` | Focus between panels |
| `Escape` | Clear search / dismiss overlay |
| `A` | Toggle Accounts |
| `R` | Reload build (hidden) |

### Tab navigation

| Key | Tab |
|---|---|
| `u` | Usage |
| `m` | MCP |
| `s` | Cycle |
| `c` | Capacity |
| `y` | Cycles |
| `x` | Test Queue |
| `l` | Leaderboard |
| `a` | Audit |
| `M` | Mission Control |
| `w` | Wire |
| `g` | Rules |
| `p` | Projects |
| `v` | Advisor |
| `i` | Inbox |
| `t` | Analytics |
| `h` | Toggle Health panel |

### Cycle navigation

| Key | Action |
|---|---|
| `[` / `]` | Previous / next cycle |
| `0` | Toggle all cycles view |

### Cycle tab (task management)

| Key | Action |
|---|---|
| `n` | New task |
| `Enter` | Edit item |
| `x` | Toggle done |
| `r` | Roll item to next cycle |
| `d` | Delete item |
| `b` | Assign to block |
| `/` | Filter |
| `a` | Show all |
| `i` | Import sessions |

### Analytics tab (time windows)

| Key | Window |
|---|---|
| `1` | 24 hours |
| `2` | 72 hours |
| `3` | 1 week |
| `4` | 1 month |

### Active Sessions / Session History

| Key | Action |
|---|---|
| `Enter` / `f` | Focus Warp terminal (Active Sessions) or drill down (Session History) |
| `t` | Toggle token breakdown (in drill-down view) |

## CLI

```bash
# Launch TUI
token-watch

# Print compact capacity snapshot (JSON)
token-watch --snapshot

# Advisor insights (text)
token-watch --advisor

# Advisor insights (JSON)
token-watch --advisor --json

# Look up session by CCID or UUID prefix
token-watch -s <CCID>

# Session with resume context
token-watch -s <CCID> --context

# List recent sessions
token-watch -l
token-watch --list
```

## Roadmap

- [x] Export session history to CSV (v0.8)
- [x] Alert notifications (v0.8)
- [x] Multi-account capacity view (v0.9)
- [x] Cycle Monitor (v0.12)
- [x] Auto-refresh all tabs (v0.13)
- [x] Wire messaging (v0.14)
- [x] Mission Control (v0.14)
- [x] Analytics / coaching engine (v0.15)
- [x] Advisor + Inbox (v0.15)
- [x] Token Access Control (v0.15)
- [x] Rules tab (v0.16)
- [x] Projects board (v0.16)
- [x] Leaderboard (v0.16)
- [x] Audit view (v0.16)
- [ ] Per-session cost estimation (partial -- fleet-level exists)
- [ ] Prompt-level analytics
- [ ] Nested row expansion in Session History

## License

MIT
