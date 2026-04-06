#!/bin/bash
# Token tracker — records usage snapshots per tool call.
# Reads rate limit data from the last statusline render.
# Writes to per-session JSONL ledger + global persistent ledger.

BUDGET_FILE="$HOME/.claude/token-budget.json"
LEDGER="/tmp/claude-token-ledger-${PPID}.jsonl"
STATE="/tmp/claude-token-state-${PPID}"
DEBUG="/tmp/statusline-debug.json"
GLOBAL_LEDGER="$HOME/.claude/logs/token-ledger.jsonl"
DIRECTIVE_FILE="/tmp/claude-directive-${PPID}"

# ── Ensure directive file always exists ──────────────────────────
# Claude sets a proper one on first interaction; this is a fallback
# so the statusline and auto-register never see an empty name.
if [ ! -f "$DIRECTIVE_FILE" ]; then
  echo "unnamed session" > "$DIRECTIVE_FILE"
fi

# Read current usage from last statusline render
if [ ! -f "$DEBUG" ]; then exit 0; fi

five_pct=$(jq -r '.rate_limits.five_hour.used_percentage // empty' "$DEBUG" 2>/dev/null)
[ -z "$five_pct" ] && exit 0

now=$(date +%s)
now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
directive=$(cat "$DIRECTIVE_FILE" 2>/dev/null || echo "unknown")
seven_pct=$(jq -r '.rate_limits.seven_day.used_percentage // 0' "$DEBUG" 2>/dev/null)

# Read tool info from stdin (hook input)
input=$(cat)
tool=$(echo "$input" | jq -r '.tool_name // "unknown"' 2>/dev/null)

# Get session start snapshot
if [ ! -f "$STATE" ]; then
  echo "${five_pct} ${now}" > "$STATE"
fi

start_pct=$(awk '{print $1}' "$STATE")
start_time=$(awk '{print $2}' "$STATE")
delta_pct=$(echo "$five_pct - $start_pct" | bc 2>/dev/null || echo "0")
elapsed_min=$(echo "($now - $start_time) / 60" | bc 2>/dev/null || echo "1")
[ "$elapsed_min" = "0" ] && elapsed_min=1
burn_rate=$(echo "scale=2; $delta_pct / $elapsed_min" | bc 2>/dev/null || echo "0")

# Log to per-session ledger + global ledger
entry="{\"ts\":\"${now_iso}\",\"epoch\":${now},\"type\":\"tool_use\",\"session\":\"cc-${PPID}\",\"tool\":\"${tool}\",\"five_pct\":${five_pct},\"seven_pct\":${seven_pct},\"delta_from_start\":${delta_pct},\"burn_rate_per_min\":${burn_rate},\"directive\":\"${directive}\"}"
echo "$entry" >> "$LEDGER"
echo "$entry" >> "$GLOBAL_LEDGER"

# Check budget
if [ -f "$BUDGET_FILE" ]; then
  enabled=$(jq -r '.enabled' "$BUDGET_FILE" 2>/dev/null)
  [ "$enabled" != "true" ] && exit 0

  hard_stop=$(jq -r '.hard_stop_at_pct' "$BUDGET_FILE" 2>/dev/null)
  burn_alert=$(jq -r '.burn_rate_alert_pct_per_min' "$BUDGET_FILE" 2>/dev/null)

  # Check if session exceeded its budget
  over=$(echo "$delta_pct > $hard_stop" | bc 2>/dev/null)
  if [ "$over" = "1" ]; then
    cat <<BLOCK
{"decision":"block","reason":"TOKEN BUDGET EXCEEDED: This session used ${delta_pct}% (limit: ${hard_stop}%). Started at ${start_pct}%, now at ${five_pct}%. Close this session or raise the budget in ~/.claude/token-budget.json"}
BLOCK
    exit 0
  fi

  # Warn on high burn rate
  hot=$(echo "$burn_rate > $burn_alert" | bc 2>/dev/null)
  if [ "$hot" = "1" ]; then
    echo "{\"decision\":\"warn\",\"reason\":\"HIGH BURN RATE: ${burn_rate}%/min. Session used ${delta_pct}% in ${elapsed_min}min. Budget: ${hard_stop}%\"}"
    exit 0
  fi
fi

exit 0
