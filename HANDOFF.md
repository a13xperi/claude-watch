# claude-watch Handoff — Pick Up Here

## What This Is
A real-time terminal dashboard for monitoring Claude Code token usage. Lives at `~/projects/claude-watch/` and is symlinked to `~/bin/claude-watch`.

## Current State (v0.2.1 — shipped to GitHub)

**Working features:**
- Token Monitor header (5h/7d bars, reset countdowns)
- Urgent Alerts (token waste warnings <30m before reset)
- Active Sessions (PID, age, used%, status, source, directive)
- Active Calls (last 3 tool calls per session)
- Session History (scrollable, 911 sessions, grouped by day, source/model/tokens)
- Tool Call Feed (scrollable, all tool events with Δ5h% tick detection)
- Tool Frequency (call counts)
- Passive Drain (drain events with Normal/Check/Spike status)

**Two versions:**
- `claude-watch` → Textual TUI (scrollable, interactive) — `claude_watch_tui.py`
- `claude-watch-rich` → Rich Live (static fallback) — `claude_watch.py`
- Shared data layer: `claude_watch_data.py`

**Symlinks in ~/bin/:**
- `claude-watch` → `~/projects/claude-watch/claude_watch_tui.py`
- `claude-watch-rich` → `~/projects/claude-watch/claude_watch.py`
- `claude_watch_data.py` → symlinked to ~/bin/ too
- `claude_watch_tui.tcss` → symlinked to ~/bin/ too

**Also deployed to:**
- `~/.claude/scripts/claude-watch` (Rich version, used by old references)
- `~/.claude/scripts/claude_watch_data.py` (data layer copy)

**Hook:** `~/.claude/hooks/token-tracker.sh` — PreToolUse hook that logs to `~/.claude/logs/token-ledger.jsonl`

## What To Build Next (v0.3 — plan approved)

Full plan at: `~/.claude/plans/temporal-stargazing-raven.md`

### Build order:

**1. Account awareness** (~5 min)
- Read `~/.claude/accounts.json` → show active account (A/B/C) + name + lane in header
- New function `_get_active_account()` in `claude_watch_data.py`
- Replace "Budget: 15% per session" line in header with account info

**2. Tool input capture** (~10 min)
- Extend `~/.claude/hooks/token-tracker.sh` to capture 80-char snippet of tool_input
- Add `tool_snippet` field to ledger entries
- Update Active Calls + Tool Call Feed to show "Bash: python3 ..." instead of just "Bash"

**3. Token pacing** (~10 min)
- New `_token_pacing()` in data layer — average last 5 drain burn rates
- Show "100% in ~18m at 0.6%/min | Reset in 45m" in header
- Handle edge cases: at limit, no drain data, reset imminent

**4. Session drill-down** (~20 min)
- New `_get_session_turns(session_id)` — parse transcript into per-turn token breakdown
- New `SessionDrillDown(Screen)` in Textual — pushed on Enter, popped on Escape
- Need to store `project_dir` in session index so we can find transcripts across all project dirs
- Columns: # | Tokens | ~5h% | Model | Tools | Prompt preview

### Known issues to fix along the way:
- "idle" label should show elapsed time (`· 19m`) not just `● idle`
- Session index has duplicates (append-only, no dedup on rebuild)
- Tool Call Feed source detection is heuristic (from directive text), not from project dir

## How To Verify Current State

```bash
# Both versions should launch without errors:
cd ~/projects/claude-watch && python3 claude_watch_tui.py  # Textual
cd ~/projects/claude-watch && python3 claude_watch.py       # Rich

# Data layer should work:
python3 -c "from claude_watch_data import _get_session_history; print(len(_get_session_history()), 'sessions')"

# Session index exists:
wc -l ~/.claude/logs/session-index.jsonl

# Hook is registered:
grep token-tracker ~/.claude/settings.json
```

## Key Context
- Python 3.9.6 (no 3.10+ type hints like `X | None`)
- Textual 8.2.3 installed
- DataTable `add_column(width=N)` works for fixed widths
- `_build_or_update_index()` scans ALL project dirs under `~/.claude/projects/`
- Session exclusion is disabled — all sessions show in history including current
- The 5h window token budget is the main constraint; 1% ≈ 5,500 output tokens

## Additional v0.3 Requirements (added end of session)

### 5. Granular Paperclip Source Identification

**Problem:** Source shows "paperclip" but Alex runs multiple companies (KAA, Delphi, Frank-Pilot, personal). Need to know which company and which agent.

**Data available:**
- Directory path: `paperclip-instances-default-projects-{project_id}-{agent_id}--default`
- Directive: `"You are agent {agent_uuid}"` 
- 10 unique project IDs exist across directories

**Approach:** 
- Create a mapping file `~/projects/claude-watch/paperclip_agents.json`:
  ```json
  {
    "projects": {
      "790f4e78": {"company": "KAA", "name": "KAA Landscape"},
      "e0c9db01": {"company": "Delphi", "name": "Delphi OS"},
      ...
    },
    "agents": {
      "fc5b4860": {"name": "DevOps Health", "role": "infra monitoring"},
      "9a47c71c": {"name": "Morning Brief", "role": "daily synthesis"},
      "5e806760": {"name": "Monitor Agent", "role": "service monitoring"},
      ...
    }
  }
  ```
- Alex fills in the company/agent names (or we auto-discover from Paperclip API)
- `_parse_transcript()` extracts project_id + agent_id from directory path
- Source display: `KAA/DevOps` instead of just `paperclip`

### 6. Usage Metrics / Productivity Dashboard

**Problem:** Can't see aggregate token consumption by source/company/agent.

**What to show:**
- Total tokens by source (cli vs paperclip vs openclaw)
- Breakdown by company within paperclip
- Breakdown by agent within company
- % of 7d budget consumed by each
- Session count and avg tokens/session per source
- "Productivity" signal: tool calls per session, output tokens per tool call

**Implementation:**
- New `_usage_metrics()` function in data layer — aggregates from session index
- New panel or screen in Textual: "Usage Metrics" (maybe a separate tab/screen, not inline)
- Could also show as a summary row at the bottom of Session History

**Example display:**
```
── Usage by Source (last 7 days) ──────────────────
Source        Sessions  Tokens    %of7d  Avg/Session
cli                 5   450.2k   8.2%     90.0k
paperclip         340    85.3k   1.6%      0.3k
  KAA/DevOps      120    32.1k   0.6%      0.3k
  KAA/Morning      80    28.4k   0.5%      0.4k
  Delphi/Monitor  140    24.8k   0.5%      0.2k
openclaw           12    16.6k   0.3%      1.4k
atlas-be            4     8.2k   0.1%      2.1k
```
