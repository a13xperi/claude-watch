# token-watch v0.3: Prompt-Level Analytics

## Context

Token cost is heavily concentrated in a few large turns (top 3 turns = 20% of session tokens, top 10 = 42%). Users need per-prompt visibility to:
1. Identify which prompts burn the most tokens
2. Compare cost across models (Sonnet vs Opus)
3. Make informed decisions about model routing
4. Understand what types of work are expensive vs cheap

All data exists in transcript files — each assistant entry has `output_tokens`, `model`, `stop_reason`, and the preceding user entry has the prompt text.

## Key Data Points (from real session analysis)

- 1% of 5h window ≈ 5,500 output tokens
- Most expensive single turn observed: 4,203 tokens (~0.8% of 5h)
- 40% of turns are <100 tokens (cheap chat)
- 9% of turns are >500 tokens (expensive builds/writes)
- Tool-use chains (Write, Edit) are the costliest turn types

## Features

### 1. Session Drill-Down View

Select a session in Session History → Enter → per-turn breakdown.

**Columns:** `# | Tokens | ~5h% | Model | Tools | Prompt preview`

Sorted by most expensive first. Escape to go back.

### 2. Expensive Turns Feed

Top 20 costliest individual turns across ALL sessions.

**Columns:** `Tokens | ~5h% | Session | Model | Tools | Prompt`

### 3. Model Cost Dashboard

```
Model    Turns   Avg Tokens   Total Tokens   Avg 5h%/turn
opus     45      312          14,040         0.06%
sonnet   28      198          5,544          0.04%
```

### 4. Fix "idle" Status Label

Show elapsed time `· 19m` instead of misleading `● idle` during long thinking.

## Implementation Order

1. Fix idle label
2. `_get_session_turns(session_id)` in data layer
3. Session Drill-Down in Textual (Enter/Escape)
4. Extend index with `top_turns` per session
5. `_get_expensive_turns()` + Expensive Turns view
6. `_model_stats()` + dashboard widget
