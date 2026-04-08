# claude-watch Handoff — Pick Up Here

## What This Is
Real-time terminal dashboard for monitoring Claude Code token usage. GitHub: a13xperi/claude-watch

## Current State (v0.15 — Pomodoro System + Audit + Skills)

**v0.15 features (shipped 2026-04-08):**
- **Full Pomodoro system**: 10 x 30-min blocks per 5h cycle with real per-block stats
  - `_get_pomodoro_stats(cycle_id)` — slices token ledger + sessions into 30-min buckets
  - `_get_current_pomodoro()` — returns current block number (1-10)
  - `P9/10` indicator in cycle banner (color-coded: cyan→white→yellow→red)
  - Mini Pomodoro strip in burndown right panel: `P: ████████▓░ 9/10`
  - Per-block budget tracking: `P9: 0.0% (budget: 10%)`
  - Cycle detail sessions grouped by Pomodoro block with separator rows
  - Accomplishment summary in Cycle Monitor showing what got done per block
  - Block assign (`b` key) — modal to assign cycle items to Pomodoro blocks
  - Auto-claim notifications when block transitions
- **Auto-populate cycle items** from session data on window boundary
- **Test detail screen**: Enter on test queue item → full detail with verify instructions, p/f/s keys
- **Audit tab** (`a` key): Full cross-cycle audit view via `_build_full_audit()`
- **Wire tab** (`w` key): Inter-session messaging
- **Mission Control** (`M` key): Everything built, grouped by company/project

**v0.7-v0.14 features (also shipped this session):**
- Token Access Control, Rules tab, cycle source breakdown
- 3-bar banner (Time/5h/7d), 8-row burndown chart
- Pomodoro gridlines, Navigation screen
- Cycle Monitor with freeform items, project selector, lanes
- Multi-account capacity view, CSV export, system notifications
- Session history with tool call sub-rows, click-to-focus

**Skills created/verified:**
- `/audit` — CLI audit reports without TUI
- `/close-cycle` — cycle report + roll forward
- `/park` — capture ideas to build_ledger

## What's Next

### Remaining backlog (from Notion):
1. Cycle navigator visible UI polish (Medium)
2. Session monitor LaunchAgent (Medium)
3. Better test hints — project-specific verify instructions (Medium)
4. Smarter lane auto-assignment in Cycle Monitor (Medium)
5. Re-seed build_ledger with correct git timestamps (Medium)

### Atlas (separate repos):
- Portal PRs #168, #166 need a11y fix before merge
- Portal PR #170 (Anil package) ready to merge
- 13 test items in test queue from push-to-test
- 7 portal + 6 backend GitHub issues open

## Key Files
| File | Purpose |
|---|---|
| claude_watch_data.py | Data layer — Pomodoro stats, cycle items, Paperclip API, audit |
| claude_watch_tui.py | All Textual UI (~5800 lines) |
| claude_watch_tui.tcss | Styles |
| paperclip_agents.json | UUID → company/agent name mapping |

## Start Command
```
Pick up the latest handoff
```
