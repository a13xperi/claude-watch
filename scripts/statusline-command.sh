#!/usr/bin/env bash
# Battlestation statusline renderer
# Called by Claude Code with JSON on stdin (rate limits, model, effort)
# Output: multi-line status for the terminal statusline area
#
# Refactored from ~/.claude/statusline-command.sh to use battlestation libs.
# Rendering logic stays inline — it's display-specific and doesn't belong in libs.

set -o pipefail

# ── Bootstrap libs ──
BATTLESTATION_HOME="${BATTLESTATION_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
source "$BATTLESTATION_HOME/lib/config.sh"
source "$BATTLESTATION_HOME/lib/atomic.sh"
source "$BATTLESTATION_HOME/lib/supabase.sh"
source "$BATTLESTATION_HOME/lib/session.sh"
source "$BATTLESTATION_HOME/lib/log.sh"
source "$BATTLESTATION_HOME/lanes/accounts.sh"

# ── Read JSON input ──
input=$(cat)
atomic_write /tmp/statusline-debug.json "$input"

# ── Active account ──
account=$(bs_active_account)
_active_label=$(jq -r '.active // "?"' "$HOME/.claude/accounts.json" 2>/dev/null)

# ── Terminal width ──
cols=$(stty size </dev/tty 2>/dev/null | awk '{print $2}')
if [ -z "$cols" ] || [ "$cols" -eq 0 ] 2>/dev/null; then
  cols=$(tput cols 2>/dev/null || echo 80)
fi

# ── Model detection ──
model=$(echo "$input" | jq -r '(.model | if type == "object" then .id else . end) // empty' 2>/dev/null | sed 's/\[.*//;s/\x1b\[[0-9;]*m//g')
if [ -z "$model" ]; then
  model=$(jq -r '.model // "unknown"' ~/.claude/settings.json 2>/dev/null)
fi
case "$model" in
  *opus*)   model_short="opus" ;;
  *sonnet*) model_short="sonnet" ;;
  *haiku*)  model_short="haiku" ;;
  *)        model_short="$model" ;;
esac

# ── Effort level ──
effort=$(echo "$input" | jq -r '.effortLevel // empty')
if [ -z "$effort" ]; then
  effort=$(jq -r '.effortLevel // "medium"' ~/.claude/settings.json 2>/dev/null)
fi

# ── Per-session directive ──
directive="—"
if [ -f "/tmp/claude-directive-$PPID" ]; then
  raw=$(cat "/tmp/claude-directive-$PPID" 2>/dev/null | tr -d '\n')
  [ -n "$raw" ] && directive="$raw"
fi

# ── Company + project from CC process cwd ──
_cwd=$(lsof -a -p $PPID -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-)
_project="—"
_company="—"
case "$_cwd" in
  */atlas-portal*|*/atlas-fe*)   _project="Atlas";       _company="Delphi" ;;
  */atlas-backend*|*/atlas-be*)  _project="Atlas";       _company="Delphi" ;;
  */paperclip*)                  _project="Paperclip";   _company="Personal" ;;
  */openclaw*)                   _project="OpenClaw";    _company="Personal" ;;
  */token-watch*|*/token-watch*) _project="Token Watch";  _company="Personal" ;;
  */battlestation*)              _project="Battlestation"; _company="Personal" ;;
  */kaa*)                        _project="KAA";         _company="KAA" ;;
  */frank*)                      _project="Frank";       _company="Frank" ;;
  "$HOME"|"$HOME/")              _project="general";     _company="Personal" ;;
  *)                             _project=$(basename "$_cwd" 2>/dev/null || echo "—"); _company="Personal" ;;
esac

# ── Session budget indicator ──
budget_line=""
BUDGET_FILE="$HOME/.claude/token-budget.json"
if [ -f "$BUDGET_FILE" ]; then
  budget_enabled=$(jq -r '.enabled // false' "$BUDGET_FILE" 2>/dev/null)
  if [ "$budget_enabled" = "true" ]; then
    _hard_stop=$(jq -r '.hard_stop_at_pct // 30' "$BUDGET_FILE" 2>/dev/null)
    _alert_at=$(jq -r '.alert_at_pct // 10' "$BUDGET_FILE" 2>/dev/null)
    _caution_at=$(echo "scale=0; $_hard_stop * 70 / 100" | bc 2>/dev/null)
    _bstate="/tmp/claude-token-state-${PPID}"
    if [ -f "$_bstate" ]; then
      _b_start=$(awk '{print $1}' "$_bstate")
    fi
  fi
fi

# ── Rate limits ──
five_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
week_pct=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
five_reset=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
week_reset=$(echo "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')

# Finish budget line now that five_pct is available
if [ -n "$_b_start" ] && [ -n "$five_pct" ]; then
  _b_delta=$(echo "$five_pct - $_b_start" | bc 2>/dev/null || echo "0")
  _b_delta_int=$(printf '%.0f' "$_b_delta" 2>/dev/null || echo "0")
  _past_caution=$(echo "$_b_delta >= $_caution_at" | bc 2>/dev/null)
  _past_alert=$(echo "$_b_delta >= $_alert_at" | bc 2>/dev/null)
  if [ "$_past_caution" = "1" ]; then
    _b_icon="◉"
  elif [ "$_past_alert" = "1" ]; then
    _b_icon="◆"
  else
    _b_icon="◈"
  fi
  budget_line="${_b_icon} ${_b_delta_int}/${_hard_stop}% budget"
fi

# ── Helpers ──

format_countdown() {
  local reset_ts="$1"
  if [ -z "$reset_ts" ]; then echo "--"; return; fi
  local now diff hours mins
  now=$(date +%s)
  diff=$((reset_ts - now))
  if [ "$diff" -le 0 ]; then echo "now"; return; fi
  hours=$((diff / 3600))
  mins=$(( (diff % 3600) / 60 ))
  if [ "$hours" -gt 0 ]; then
    printf '%dh%02dm' "$hours" "$mins"
  else
    printf '%dm' "$mins"
  fi
}

make_bar() {
  local pct=$1
  local segments=$((pct / 10))
  local filled="" empty=""
  [ "$segments" -gt 0 ] && filled=$(printf '▰%.0s' $(seq 1 $segments))
  local rem=$((10 - segments))
  [ "$rem" -gt 0 ] && empty=$(printf '▱%.0s' $(seq 1 $rem))
  echo "${filled}${empty}"
}

# ── Format session + weekly bars ──

if [ -n "$five_pct" ]; then
  session_pct=$(printf '%.0f' "$five_pct")
  session_reset=$(format_countdown "$five_reset")
  bar=$(make_bar "$session_pct")
else
  session_pct="--"
  session_reset="--"
  bar="▱▱▱▱▱▱▱▱▱▱"
fi

if [ -n "$week_pct" ]; then
  weekly_pct=$(printf '%.0f' "$week_pct")
  weekly_reset=$(format_countdown "$week_reset")
  w_bar=$(make_bar "$weekly_pct")
else
  weekly_pct="--"
  weekly_reset="--"
  w_bar="▱▱▱▱▱▱▱▱▱▱"
fi

trunc_directive() {
  local max=$1
  if [ "$max" -gt 0 ] && [ "${#directive}" -gt "$max" ]; then
    directive="${directive:0:$((max - 1))}…"
  fi
}

# Build S/W lines with aligned bars
s_left="S ${session_pct}% ⟳${session_reset}"
w_left="W ${weekly_pct}% ⟳${weekly_reset}"

# Pad shorter line so bars align
s_len=${#s_left}
w_len=${#w_left}
if [ "$s_len" -gt "$w_len" ]; then
  pad=$((s_len - w_len))
  w_left="${w_left}$(printf '%*s' "$pad" '')"
elif [ "$w_len" -gt "$s_len" ]; then
  pad=$((w_len - s_len))
  s_left="${s_left}$(printf '%*s' "$pad" '')"
fi

# Check if bars fit on same line
bar_start=$(( ${#s_left} + 2 ))
if [ $((bar_start + 10)) -le "$cols" ]; then
  s_line="${s_left}  ${bar}"
  w_line="${w_left}  ${w_bar}"
else
  s_line="$s_left"
  w_line="$w_left"
fi

# ── 70% weekly usage alert ──
alert_line=""
if [ -n "$week_pct" ]; then
  week_int=$(printf '%.0f' "$week_pct")
  if [ "$week_int" -ge 70 ]; then
    best_alt=$(cat /tmp/claude-best-alt-account 2>/dev/null)
    if [ -n "$best_alt" ]; then
      alert_line="!! W${week_int}% — switch to ${best_alt}"
    else
      alert_line="!! W${week_int}% — consider switching"
    fi
  fi
fi

# ── Output core lines immediately (no network delay) ──

# Wide terminals (≥45 cols): directive gets its own line
# Narrow terminals (<45 cols): directive folds into header line
if [ "$cols" -ge 45 ] && [ "$directive" != "—" ]; then
  trunc_directive $((cols - 4))
  directive_line=$(printf "\n▸ %s" "$directive")
elif [ "$directive" != "—" ]; then
  trunc_directive $((cols - ${#account} - ${#model_short} - ${#effort} - 6))
  directive_line=$(printf " | %s" "$directive")
else
  directive_line=""
fi

if [ -n "$budget_line" ]; then
  printf "%s:%s:%s%s\n▶ %s | %s | %s\n%s\n%s\n%s" \
    "$account" "$model_short" "$effort" \
    "$directive_line" \
    "$PPID" "$_company" "$_project" \
    "$budget_line" \
    "$s_line" \
    "$w_line"
else
  printf "%s:%s:%s%s\n▶ %s | %s | %s\n%s\n%s" \
    "$account" "$model_short" "$effort" \
    "$directive_line" \
    "$PPID" "$_company" "$_project" \
    "$s_line" \
    "$w_line"
fi

if [ -n "$alert_line" ]; then
  printf "\n⚠ %s" "$alert_line"
fi

# ── Peer sessions — read cached data ──
my_session=$(bs_session_id)

if [ -f /tmp/claude-peers.json ]; then
  peer_count=$(jq -r 'length' /tmp/claude-peers.json 2>/dev/null || echo "0")
  if [ "$peer_count" -gt 0 ]; then
    others=$(jq -r --arg me "$my_session" '[.[] | select(.session_id != $me)] | length' /tmp/claude-peers.json 2>/dev/null || echo "0")
    if [ "$others" -gt 0 ]; then
      peer_names=$(jq -r --arg me "$my_session" '[.[] | select(.session_id != $me) | (.session_id + " " + (.task_name | .[0:18]))] | join(" | ")' /tmp/claude-peers.json 2>/dev/null)
      peers_line="${others} peers: ${peer_names}"
      if [ "${#peers_line}" -gt "$((cols - 2))" ]; then
        peers_line="${others} active peers"
      fi
      printf "\n⚡ %s" "$peers_line"
    fi
  fi
fi

# ── Background: write capacity to Supabase, refresh peer cache, expire stale sessions ──

# Write capacity snapshot (throttled to once per 60s)
NOW_S=$(date +%s)
CAP_FLAG="/tmp/claude-capacity-write"
CAP_LAST=$(cat "$CAP_FLAG" 2>/dev/null || echo "0")
if [ -n "$five_pct" ] && [ -n "$week_pct" ] && [ -n "$_active_label" ] && [ "$_active_label" != "?" ]; then
  if [ $((NOW_S - CAP_LAST)) -gt 60 ]; then
    atomic_write "$CAP_FLAG" "$NOW_S"
    supa_patch "account_capacity" "account=eq.${_active_label}" \
      "{
        \"five_hour_used_pct\": ${five_pct},
        \"five_hour_resets_at\": ${five_reset:-0},
        \"seven_day_used_pct\": ${week_pct},
        \"seven_day_resets_at\": ${week_reset:-0},
        \"snapshot_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
        \"is_active\": true
      }" &>/dev/null &
  fi
fi

# Refresh peer cache in background (atomic write to avoid corruption)
(atomic_write_cmd /tmp/claude-peers.json \
  curl -s --max-time 3 \
    "${SUPA_URL}/rest/v1/session_locks?status=eq.active&select=session_id,task_name,repo,heartbeat_at,files_touched&order=claimed_at.desc" \
    -H "apikey: ${SUPA_KEY}" \
    -H "Authorization: Bearer ${SUPA_KEY}") &

# Auto-expire stale sessions (once per minute)
EXPIRY_FLAG="/tmp/claude-expiry-check"
LAST_EXPIRY=$(cat "$EXPIRY_FLAG" 2>/dev/null || echo "0")
if [ $((NOW_S - LAST_EXPIRY)) -gt 60 ]; then
  atomic_write "$EXPIRY_FLAG" "$NOW_S"
  # macOS date -v vs GNU date -d
  thirty_min_ago=$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
  supa_patch "session_locks" \
    "status=eq.active&heartbeat_at=lt.${thirty_min_ago}" \
    '{"status":"done","released_at":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}' &>/dev/null &
fi

# Compute best alt account and cache for alert line (background)
(source "$BATTLESTATION_HOME/lanes/accounts.sh" 2>/dev/null
 best=$(bs_best_alt_account 2>/dev/null)
 [ -n "$best" ] && atomic_write /tmp/claude-best-alt-account "$best") &
