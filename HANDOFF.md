# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.7 — Token Access + Rules + Pomodoro Gridlines)

**v0.7 features (shipped 2026-04-08):**
- **Token Access Control**: Panel in Usage tab showing all 23 Paperclip heartbeat agents grouped by company. Click → full toggle screen, Enter to flip on/off via Paperclip API. Suppressed run detection infers missed runs from known intervals.
- **Rules tab** (g key): Lists all 11 rules (7 hooks, 1 budget, 3 permissions). Shows trigger/block counts per cycle from permissions.jsonl + battlestation.log. Click rule → event detail.
- **Cycle source breakdown**: Token Distribution table in cycle detail view showing per-source token split within a 5h window.
- **Cycle start + end times**: Both columns in Cycles list view.
- **3-bar banner**: Top bar shows Time/5h/7d remaining with colored bars (red=low, green=plenty). Bars fill right-to-left showing remaining resources.
- **8-row burndown chart**: Taller chart filling full vertical space. Right-side info panel with Used/Left, 5h/7d bars, zones, account, verdict, budget, score.
- **Pomodoro gridlines**: Dotted vertical lines at 30-min intervals in burndown (10 blocks per 5h window).
- **Navigation screen**: "Nav" button opens full-screen tab menu for narrow windows.

**All 23 Paperclip heartbeats disabled** — re-enable via Token Access toggle or manually via API.

## What's Next — Pomodoro System

The gridlines are visual only. Next session builds the full Pomodoro execution framework:

1. Per-Pomodoro stats — `_get_pomodoro_stats(cycle_id)` slicing existing data into 30-min buckets
2. "P7/10" indicator in the top banner
3. Per-block token budget vs actual in burndown right panel
4. Pomodoro planning UI — assign tasks to blocks
5. Cycle detail grouped by Pomodoro instead of flat session list
6. Auto-claim next planned task when block completes

Default template: P1-2 plan (20%), P3-8 build (60%), P9 assign next (10%), P10 close (10%).

See memory: `project_pomodoro_system.md`

## Start Command
```
Build the Pomodoro system — read project_pomodoro_system.md memory
```

## Key Files
| File | Purpose |
|---|---|
| claude_watch_data.py | Data layer — Paperclip API, rules system, heartbeat management |
| claude_watch_tui.py | All Textual UI (~5500 lines) |
| claude_watch_tui.tcss | Styles |
| paperclip_agents.json | UUID → company/agent name mapping |
