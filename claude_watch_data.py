"""
claude-watch data layer — shared by Rich and Textual versions.
All data fetching, caching, and computation lives here.
"""

import json
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.panel import Panel
from rich.table import Table

# ── constants ────────────────────────────────────────────────────────────────

LEDGER = Path.home() / ".claude/logs/token-ledger.jsonl"
BUDGET_FILE = Path.home() / ".claude/token-budget.json"
TRANSCRIPTS_DIR = Path.home() / ".claude/projects/-Users-a13xperi"
ALL_PROJECT_DIRS = Path.home() / ".claude/projects"
SESSION_INDEX = Path.home() / ".claude/logs/session-index.jsonl"
PAPERCLIP_AGENTS_FILE = Path(__file__).parent / "paperclip_agents.json"

_PAPERCLIP_RE = re.compile(
    r"paperclip-instances-default-(?:projects|workspaces)-"
    r"([a-f0-9-]{36})-([a-f0-9-]{36})--default"
)
_PAPERCLIP_WS_RE = re.compile(
    r"paperclip-instances-default-workspaces-([a-f0-9-]{36})$"
)

_paperclip_map = {}   # type: Dict[str, Dict]
_paperclip_agents_flat = {}  # agent_uuid -> (company, name)


def _load_paperclip_map():
    global _paperclip_map, _paperclip_agents_flat
    try:
        data = json.loads(PAPERCLIP_AGENTS_FILE.read_text())
        _paperclip_map = data.get("projects", {})
        # Build flat agent UUID → (company, name) for workspace lookups
        for proj_info in _paperclip_map.values():
            company = proj_info.get("company", "?")
            for agent_uuid, name in proj_info.get("agents", {}).items():
                _paperclip_agents_flat[agent_uuid] = (company, name)
    except Exception:
        _paperclip_map = {}
        _paperclip_agents_flat = {}


_load_paperclip_map()


# ── helpers ──────────────────────────────────────────────────────────────────

def _current_pct():
    """Returns (five, seven, five_reset_ts, seven_reset_ts)."""
    try:
        r = subprocess.run(
            ["bash", "-c", "cat /tmp/statusline-debug.json 2>/dev/null"],
            capture_output=True, text=True, timeout=2,
        )
        if r.stdout.strip():
            d = json.loads(r.stdout)
            rl = d.get("rate_limits", {})
            five = rl.get("five_hour", {}).get("used_percentage", "?")
            seven = rl.get("seven_day", {}).get("used_percentage", "?")

            def _ts(raw):
                if isinstance(raw, (int, float)):
                    return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
                return raw or ""

            five_reset = _ts(rl.get("five_hour", {}).get("resets_at", ""))
            seven_reset = _ts(rl.get("seven_day", {}).get("resets_at", ""))
            return five, seven, five_reset, seven_reset
    except Exception:
        pass
    return "?", "?", "", ""


def _countdown(reset_ts):
    if not reset_ts:
        return "?"
    try:
        reset = datetime.fromisoformat(reset_ts.replace("Z", "+00:00"))
        diff = int((reset - datetime.now(timezone.utc)).total_seconds())
        if diff <= 0:
            return "resetting..."
        h, rem = divmod(diff, 3600)
        m = rem // 60
        return f"{h}h{m:02d}m"
    except Exception:
        return "?"


def _reset_day(reset_ts):
    if not reset_ts:
        return "?"
    try:
        dt = datetime.fromisoformat(reset_ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime(f"%a %b {dt.day}")
    except Exception:
        return "?"


def _abbrev_model(model):
    if not model:
        return "?"
    m = model.lower()
    # Extract context tier if present (e.g. opus[1m] → opus:1m)
    tier = ""
    if "[" in m and "]" in m:
        tier = ":" + m[m.index("[") + 1:m.index("]")]
    if "opus" in m:
        return "opus" + tier
    if "sonnet" in m:
        return "sonnet" + tier
    if "haiku" in m:
        return "haiku" + tier
    return model[:10]


def _budget():
    try:
        if BUDGET_FILE.exists():
            return json.loads(BUDGET_FILE.read_text()).get("per_session_pct", 15)
    except Exception:
        pass
    return 15


def _active_sessions():
    """Return list of (pid, age_str, directive, delta) for active claude sessions."""
    sessions = []
    try:
        r = subprocess.run(
            ["ps", "ax", "-o", "pid,etime,command"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, etime, cmd = parts
            if cmd.strip() in ("claude", "/usr/local/bin/claude", "/opt/homebrew/bin/claude"):
                directive = ""
                try:
                    directive = Path(f"/tmp/claude-directive-{pid}").read_text().strip()
                except Exception:
                    pass
                delta = "?"
                try:
                    state_parts = Path(f"/tmp/claude-token-state-{pid}").read_text().split()
                    start_pct = float(state_parts[0])
                    start_epoch = float(state_parts[1]) if len(state_parts) > 1 else 0
                    cur = float(_current_pct()[0])
                    raw_delta = round(cur - start_pct, 1)
                    # Fix ghost session: if session just started and shows huge delta,
                    # it's measuring global drift, not actual consumption
                    age_secs = time.time() - start_epoch if start_epoch else 999
                    if raw_delta < 0:
                        delta = "reset"  # 5h window reset during session
                    elif age_secs < 120 and raw_delta > 5:
                        delta = "new"
                    else:
                        delta = f"+{raw_delta}%"
                except Exception:
                    pass
                source = _detect_source(pid)
                sessions.append((pid, etime, directive or "—", delta, source))
    except Exception:
        pass
    return sessions


def _active_pids():
    """Return set of active cc-{PID} session IDs."""
    return {f"cc-{item[0]}" for item in _active_sessions()}


def _session_last_activity(session_id):
    """Return (seconds_ago, last_tool) for a session from the ledger."""
    entries = _load_ledger(last_n=200)
    sid = f"cc-{session_id}"
    now = datetime.now(timezone.utc)
    for e in reversed(entries):
        if e.get("session") == sid and e.get("type") == "tool_use":
            try:
                ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                secs = int((now - ts).total_seconds())
                tool = e.get("tool", "?")
                if tool.startswith("mcp__claude_ai_"):
                    tool = "mcp:" + tool.replace("mcp__claude_ai_", "").replace("__", "/")
                elif tool.startswith("mcp__"):
                    tool = "mcp:" + tool[5:]
                return secs, tool
            except Exception:
                pass
    return None, None


def _detect_source(pid):
    """Detect where a Claude session was launched from via parent process."""
    try:
        r = subprocess.run(
            ['ps', '-p', pid, '-o', 'ppid='],
            capture_output=True, text=True, timeout=2,
        )
        ppid = r.stdout.strip()
        if not ppid:
            return '?'
        r2 = subprocess.run(
            ['ps', '-p', ppid, '-o', 'command='],
            capture_output=True, text=True, timeout=2,
        )
        parent_cmd = r2.stdout.strip().lower()
        if 'paperclip' in parent_cmd:
            return 'paperclip'
        if 'atlas' in parent_cmd:
            return 'atlas'
        if 'electron' in parent_cmd or 'claude desktop' in parent_cmd:
            return 'desktop'
        if 'cron' in parent_cmd or 'launchd' in parent_cmd:
            return 'scheduled'
        if any(sh in parent_cmd for sh in ('zsh', 'bash', 'fish', 'sh ')):
            return 'cli'
        return 'cli'
    except Exception:
        return '?'


# ── ledger ───────────────────────────────────────────────────────────────────

_ledger_cache_time = 0.0
_ledger_cache = []


def _load_ledger(last_n=None):
    """Load ledger entries. Always loads all entries, caches by mtime."""
    global _ledger_cache_time, _ledger_cache
    if not LEDGER.exists():
        return []
    mtime = LEDGER.stat().st_mtime
    if mtime == _ledger_cache_time and _ledger_cache is not None:
        entries = _ledger_cache
    else:
        entries = []
        try:
            with open(LEDGER) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
        _ledger_cache = entries
        _ledger_cache_time = mtime
    if last_n is not None:
        return entries[-last_n:]
    return entries


def _interpolate_five_pct(target_ts):
    best, best_diff = None, float("inf")
    for e in _load_ledger():
        pct = e.get("five_pct")
        if pct is None:
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
            diff = abs((ts - target_ts).total_seconds())
            if diff < best_diff:
                best_diff, best = diff, pct
        except Exception:
            pass
    return best


# ── session index ────────────────────────────────────────────────────────────

_index_cache = {}
_index_loaded = False
_index_building = False
_index_thread = None


def _load_index():
    global _index_cache, _index_loaded
    cache = {}
    if SESSION_INDEX.exists():
        try:
            with open(SESSION_INDEX) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        sid = obj.get("session_id")
                        if sid:
                            cache[sid] = obj
                    except Exception:
                        pass
        except Exception:
            pass
    _index_cache = cache
    _index_loaded = True
    return cache


def _parse_transcript(f):
    total_out = 0
    first_ts = last_ts = None
    slug = last_prompt = None
    model_counts = defaultdict(int)
    try:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type", "")
                ts_str = obj.get("timestamp", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    except Exception:
                        pass
                if t == "assistant":
                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    out = usage.get("output_tokens", 0)
                    total_out += out
                    mdl = msg.get("model", "")
                    if mdl and not mdl.startswith("<"):
                        model_counts[mdl] += out
                if t == "system" and not slug:
                    s = obj.get("slug", "")
                    if s:
                        slug = s
                if t == "last-prompt":
                    lp = obj.get("lastPrompt", "")
                    if lp:
                        last_prompt = lp
    except Exception:
        return None
    if first_ts is None:
        return None
    directive = (last_prompt[:40] if last_prompt else None) or slug or f.stem[:8]
    dominant_model = max(model_counts, key=model_counts.get) if model_counts else ""
    # Derive source from project directory name
    parent = f.parent.name
    m = _PAPERCLIP_RE.search(parent)
    mws = _PAPERCLIP_WS_RE.search(parent)
    if m:
        proj_uuid, agent_uuid = m.group(1), m.group(2)
        proj_info = _paperclip_map.get(proj_uuid, {})
        company = proj_info.get("company", proj_uuid[:6])
        agent_name = proj_info.get("agents", {}).get(agent_uuid, agent_uuid[:6])
        source = f"{company}/{agent_name}"
    elif mws:
        agent_uuid = mws.group(1)
        pair = _paperclip_agents_flat.get(agent_uuid)
        if pair:
            source = f"{pair[0]}/{pair[1]}"
        else:
            source = f"pp/{agent_uuid[:6]}"
    elif "atlas-backend" in parent:
        source = "atlas-be"
    elif "atlas-portal" in parent:
        source = "atlas-fe"
    elif "openclaw" in parent:
        source = "openclaw"
    elif "frank-pilot" in parent:
        source = "frank"
    elif parent == "-Users-a13xperi":
        source = "cli"
    else:
        source = "agent"

    return {
        "session_id": f.stem,
        "first_ts": first_ts.isoformat(),
        "last_ts": (last_ts or first_ts).isoformat(),
        "output_tokens": total_out,
        "slug": slug or "",
        "directive": directive,
        "model": dominant_model,
        "source": source,
        "project_dir": str(f.parent),
        "file_mtime": f.stat().st_mtime,
    }


def _build_or_update_index():
    global _index_building, _index_cache
    if _index_building:
        return
    _index_building = True
    try:
        known = dict(_index_cache)
        new_entries = []
        for proj_dir in ALL_PROJECT_DIRS.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                sid = f.stem
                existing = known.get(sid)
                if existing and f.stat().st_mtime <= existing.get("file_mtime", 0):
                    continue
                result = _parse_transcript(f)
                if result:
                    new_entries.append(result)
                    known[sid] = result
        if new_entries:
            # Rewrite full index (deduped by session_id) instead of appending
            with open(SESSION_INDEX, "w") as fh:
                for entry in known.values():
                    fh.write(json.dumps(entry) + "\n")
            _index_cache = dict(known)
    except Exception:
        pass
    finally:
        _index_building = False


def _ensure_index():
    global _index_thread
    if not _index_loaded:
        _load_index()
    if _index_thread is None or not _index_thread.is_alive():
        _index_thread = threading.Thread(target=_build_or_update_index, daemon=True)
        _index_thread.start()


def _get_session_history():
    _ensure_index()
    # Don't exclude any sessions — show everything in history.
    # The current session appears in both Active Sessions and Session History.
    # This is better than sessions mysteriously disappearing.
    current_session_id = None

    today = datetime.now(timezone.utc).astimezone().date()
    sessions = []

    for sid, entry in _index_cache.items():
        if sid == current_session_id:
            continue
        try:
            first_ts = datetime.fromisoformat(entry["first_ts"])
            last_ts = datetime.fromisoformat(entry["last_ts"])
        except Exception:
            continue

        session_date = last_ts.astimezone().date()
        secs = int((last_ts - first_ts).total_seconds())
        if secs > 86400:  # >24h — multi-day transcript, duration meaningless
            dur_str = "—"
        else:
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            dur_str = f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

        pct_str = "—"
        if session_date == today:
            ps = _interpolate_five_pct(first_ts)
            pe = _interpolate_five_pct(last_ts)
            if ps is not None and pe is not None:
                try:
                    d_pct = round(float(pe) - float(ps), 1)
                    if d_pct < -5:
                        pct_str = "reset"  # 5h window reset during session
                    else:
                        pct_str = f"+{d_pct}%" if d_pct >= 0 else f"{d_pct}%"
                except Exception:
                    pass

        sessions.append({
            "session_id": sid,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "output_tokens": entry.get("output_tokens", 0),
            "pct_str": pct_str,
            "dur_str": dur_str,
            "directive": entry.get("directive", "—"),
            "slug": entry.get("slug", ""),
            "model": entry.get("model", ""),
            "source": entry.get("source", "?"),
            "date": session_date,
        })

    sessions.sort(key=lambda s: (s["last_ts"], s["session_id"]), reverse=True)
    return sessions


# ── session drill-down ───────────────────────────────────────────────────────

def _find_transcript(session_id):
    """Find transcript file for a session_id, checking index first then scanning."""
    entry = _index_cache.get(session_id)
    if entry and entry.get("project_dir"):
        p = Path(entry["project_dir"]) / f"{session_id}.jsonl"
        if p.exists():
            return p
    # Fallback: scan all project dirs
    for proj_dir in ALL_PROJECT_DIRS.iterdir():
        if not proj_dir.is_dir():
            continue
        p = proj_dir / f"{session_id}.jsonl"
        if p.exists():
            return p
    return None


def _get_session_turns(session_id):
    """Parse transcript into per-turn breakdown.
    Returns list of dicts: turn_num, tokens_in, tokens_out, model, tools, prompt_preview
    """
    f = _find_transcript(session_id)
    if not f:
        return []

    turns = []
    turn_num = 0
    current_tools = []
    last_prompt = ""

    try:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                t = obj.get("type", "")

                if t == "human":
                    # Start of a new turn — save previous if any
                    last_prompt = ""
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            last_prompt = content[:60]
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    last_prompt = c.get("text", "")[:60]
                                    break

                elif t == "assistant":
                    turn_num += 1
                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    tokens_in = usage.get("input_tokens", 0)
                    tokens_out = usage.get("output_tokens", 0)
                    model = _abbrev_model(msg.get("model", ""))

                    # Extract tool names from content blocks
                    content = msg.get("content", [])
                    tools = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tools.append(_shorten_tool(block.get("name", "?")))

                    # Estimate 5h% contribution (~5500 output tokens = 1%)
                    pct_est = tokens_out / 5500.0 if tokens_out else 0

                    turns.append({
                        "turn": turn_num,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "pct_est": round(pct_est, 2),
                        "model": model,
                        "tools": ", ".join(tools) if tools else "—",
                        "prompt": last_prompt or "—",
                    })

    except Exception:
        pass

    return turns


# ── usage metrics ────────────────────────────────────────────────────────────

def _get_usage_metrics(days=7):
    """Aggregate output tokens by source over the last N days.
    Returns (metrics_list, total_output_tokens).
    Each metric: source, output_tokens, sessions, avg_tokens, pct_of_total.
    """
    _ensure_index()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    by_source = defaultdict(lambda: {"output_tokens": 0, "sessions": 0})
    total_output = 0

    for sid, entry in _index_cache.items():
        try:
            last_ts = datetime.fromisoformat(entry["last_ts"])
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if last_ts < cutoff:
            continue

        src = entry.get("source", "?")
        tokens = entry.get("output_tokens", 0)
        by_source[src]["output_tokens"] += tokens
        by_source[src]["sessions"] += 1
        total_output += tokens

    metrics = []
    for src, data in sorted(by_source.items(), key=lambda x: x[1]["output_tokens"], reverse=True):
        pct = (data["output_tokens"] / total_output * 100) if total_output else 0
        avg = data["output_tokens"] // data["sessions"] if data["sessions"] else 0
        metrics.append({
            "source": src,
            "output_tokens": data["output_tokens"],
            "sessions": data["sessions"],
            "avg_tokens": avg,
            "pct_of_total": pct,
        })

    return metrics, total_output


# ── skill stats ──────────────────────────────────────────────────────────────

def _get_skill_stats():
    """Return list of (skill_name, count, last_used_str) from ledger."""
    entries = _load_ledger(last_n=2000)
    skill_counts = defaultdict(int)
    skill_last = {}  # type: Dict[str, str]
    for e in entries:
        if e.get("type") != "tool_use":
            continue
        tool = e.get("tool", "")
        snippet = e.get("tool_snippet", "")
        if tool == "Skill":
            # snippet is the skill name (e.g. "claim-task", "paperclip")
            name = "/" + snippet.strip().split()[0].lstrip("/") if snippet.strip() else "/unknown"
            skill_counts[name] += 1
            ts = e.get("ts", "")
            try:
                skill_last[name] = datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).astimezone().strftime("%H:%M:%S")
            except Exception:
                skill_last[name] = "?"
    result = []
    for name, count in sorted(skill_counts.items(), key=lambda x: x[1], reverse=True):
        result.append((name, count, skill_last.get(name, "?")))
    return result


def make_skills_panel():
    stats = _get_skill_stats()
    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 1), expand=True)
    t.add_column("Skill", overflow="ellipsis", no_wrap=True, ratio=3)
    t.add_column("Calls", min_width=5, justify="right", no_wrap=True)
    t.add_column("Last", min_width=6, no_wrap=True)
    if not stats:
        t.add_row("[dim]no skill calls yet[/dim]", "", "")
    else:
        for name, count, last in stats[:10]:
            t.add_row(name, str(count), f"[dim]{last}[/dim]")
    return Panel(t, title="[bold]Skills[/bold]  [dim](from ledger)[/dim]", border_style="magenta")


# ── PID mapping (transcript UUID → cc-PID) ───────────────────────────────────

_pid_map_cache = {}   # type: Dict[str, str]  # transcript UUID → cc-PID
_pid_map_time = 0.0


def _build_pid_map():
    """Build mapping from transcript session UUIDs to cc-PIDs using ledger timestamps."""
    global _pid_map_cache, _pid_map_time
    # Only rebuild every 10s
    now = time.time()
    if now - _pid_map_time < 10 and _pid_map_cache:
        return _pid_map_cache
    _pid_map_time = now

    entries = _load_ledger(last_n=5000)
    # Build per-PID time ranges from ledger
    pid_ranges = {}  # type: Dict[str, Tuple[datetime, datetime]]
    for e in entries:
        sid = e.get("session", "")
        if not sid.startswith("cc-"):
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if sid not in pid_ranges:
            pid_ranges[sid] = (ts, ts)
        else:
            first, last = pid_ranges[sid]
            if ts < first:
                first = ts
            if ts > last:
                last = ts
            pid_ranges[sid] = (first, last)

    # Match transcript sessions to PIDs by overlapping time ranges
    result = {}
    for uuid, entry in _index_cache.items():
        try:
            t_first = datetime.fromisoformat(entry["first_ts"])
            t_last = datetime.fromisoformat(entry["last_ts"])
            if t_first.tzinfo is None:
                t_first = t_first.replace(tzinfo=timezone.utc)
            if t_last.tzinfo is None:
                t_last = t_last.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        best_pid = None
        best_overlap = 0
        for pid_sid, (p_first, p_last) in pid_ranges.items():
            overlap_start = max(t_first, p_first)
            overlap_end = min(t_last, p_last)
            overlap = max(0, (overlap_end - overlap_start).total_seconds())
            if overlap > best_overlap:
                best_overlap = overlap
                best_pid = pid_sid
        if best_pid and best_overlap > 5:
            result[uuid] = best_pid

    _pid_map_cache = result
    return result


# ── call history (aggregated per session from ledger) ────────────────────────

def _get_call_history():
    """Aggregate tool calls per session from ledger. Returns list of dicts sorted by last activity.
    Includes recent tool details (merged from former Last Tool Activity panel).
    """
    entries = _load_ledger(last_n=5000)
    tool_events = [e for e in entries if e.get("type") == "tool_use"]
    if not tool_events:
        return []

    sessions = {}  # type: Dict[str, Dict]
    for e in tool_events:
        sid = e.get("session", "?")
        if sid not in sessions:
            sessions[sid] = {
                "session": sid,
                "calls": 0,
                "tools": defaultdict(int),
                "first_ts": e.get("ts", ""),
                "last_ts": e.get("ts", ""),
                "directive": e.get("directive", "—"),
                "five_pct_start": e.get("five_pct"),
                "five_pct_end": e.get("five_pct"),
                "recent_tools": [],
                "model": "?",
            }
        s = sessions[sid]
        s["calls"] += 1
        tool = _shorten_tool(e.get("tool", "?"))
        s["tools"][tool] += 1
        s["last_ts"] = e.get("ts", s["last_ts"])
        pct = e.get("five_pct")
        if pct is not None:
            s["five_pct_end"] = pct
        mdl = e.get("model", "")
        if mdl and mdl != "?":
            s["model"] = mdl
        # Keep last 3 tool calls with snippets
        snippet = e.get("tool_snippet", "")
        s["recent_tools"].append(f"{tool}: {snippet[:20]}" if snippet else tool)
        if len(s["recent_tools"]) > 3:
            s["recent_tools"] = s["recent_tools"][-3:]

    result = []
    for sid, s in sessions.items():
        # Top 3 tools by count
        top_tools = sorted(s["tools"].items(), key=lambda x: x[1], reverse=True)[:3]
        tools_str = ", ".join(f"{t}({c})" for t, c in top_tools)

        # 5h% used
        try:
            delta = float(s["five_pct_end"]) - float(s["five_pct_start"])
            if delta < -5:
                pct_str = "reset"
            else:
                pct_str = f"+{delta:.1f}%" if delta >= 0 else f"{delta:.1f}%"
        except Exception:
            pct_str = "?"

        # When (last activity)
        try:
            last = datetime.fromisoformat(s["last_ts"].replace("Z", "+00:00"))
            when_str = last.astimezone().strftime("%H:%M:%S")
            when_date = last.astimezone().date()
        except Exception:
            when_str = "?"
            when_date = None

        # Source from index cache (accurate) with fallback to session ID lookup
        source = _index_cache.get(sid, {}).get("source", "cli")

        # Recent tool detail (last tool with snippet)
        recent_str = s["recent_tools"][-1] if s["recent_tools"] else "—"

        result.append({
            "session": sid,
            "source": source,
            "model": _abbrev_model(s.get("model", "?")),
            "when": when_str,
            "when_date": when_date,
            "calls": s["calls"],
            "tools_str": tools_str,
            "recent_str": recent_str,
            "pct_str": pct_str,
            "directive": s["directive"] or "—",
            "last_ts_raw": s["last_ts"],
        })

    result.sort(key=lambda x: x["last_ts_raw"], reverse=True)
    return result


# ── tool feed rows ───────────────────────────────────────────────────────────

def _shorten_tool(tool):
    if tool.startswith("mcp__claude_ai_"):
        return "mcp:" + tool.replace("mcp__claude_ai_", "").replace("__", "/")
    if tool.startswith("mcp__"):
        return "mcp:" + tool[5:]
    return tool


def _compute_tool_feed_rows(last_n=200):
    """Return list of dicts with display-ready fields for tool feed.
    Each dict: ts_str, session, tool, directive, delta_str, delta_style
    """
    entries = _load_ledger(last_n=500)
    tool_events = [e for e in entries if e.get("type") == "tool_use"][-last_n:]
    if not tool_events:
        return []

    # Build prev_pct map (first seen pct per session)
    prev_pct = {}
    for e in tool_events:
        sess = e.get("session", "?")
        pct = e.get("five_pct")
        if pct is not None and sess not in prev_pct:
            prev_pct[sess] = pct

    rows = []
    for e in reversed(tool_events):
        ts = e.get("ts", "")
        try:
            ts_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
        except Exception:
            ts_str = ts[-8:] if ts else "?"

        session = e.get("session", "?")
        tool = _shorten_tool(e.get("tool", "?"))
        directive = e.get("directive", "—") or "—"
        if directive == "unknown":
            directive = "—"

        cur_pct = e.get("five_pct")
        cumulative = e.get("delta_from_start", 0)

        prev = prev_pct.get(session)
        tick = None
        if cur_pct is not None and prev is not None:
            try:
                diff = float(cur_pct) - float(prev)
                if diff > 0:
                    tick = diff
            except Exception:
                pass
        if cur_pct is not None:
            prev_pct[session] = cur_pct

        if tick:
            delta_str = f"▲+{tick:.0f}%"
            delta_style = "bold red" if tick >= 2 else "bold yellow"
        elif cumulative:
            try:
                c = float(cumulative)
                delta_str = f"+{c:.1f}%" if c > 0 else "—"
                delta_style = "dim"
            except Exception:
                delta_str = "—"
                delta_style = "dim"
        else:
            delta_str = "—"
            delta_style = "dim"

        snippet = e.get("tool_snippet", "")
        # Strip cc- prefix from session for index lookup
        index_sid = session[3:] if session.startswith("cc-") else session
        source = _index_cache.get(index_sid, {}).get("source", "cli")
        rows.append({
            "ts_str": ts_str,
            "session": session,
            "tool": f"{tool}: {snippet[:15]}" if snippet else tool,
            "directive": directive,
            "delta_str": delta_str,
            "delta_style": delta_style,
            "source": source,
        })

    return rows


# ── drain ────────────────────────────────────────────────────────────────────

def _drain_status(drain_events):
    if not drain_events:
        return "dim", "● No drain data yet"
    last = drain_events[-1]
    try:
        delta = float(last.get("delta_5h", 0))
        burn = float(last.get("burn_rate_per_min", 0))
        sessions = int(last.get("cli_sessions", 0))
    except Exception:
        return "dim", "● Status unknown"

    if delta > 3:
        return "red", f"✖  Spike — +{delta:.0f}% in one interval. Check for runaway."
    if burn > 6:
        return "red", f"✖  Runaway — {burn:.1f}%/min burn rate detected"
    if sessions > 2:
        per = burn / sessions if sessions else burn
        return "yellow", f"▲  {sessions} sessions — ~{per:.1f}%/min each"
    return "green", f"●  Normal — {sessions} session{'s' if sessions != 1 else ''}, ~{burn:.0f}%/min"


# ── Rich panel builders (used by Rich version + Textual Static widgets) ──────

def _token_pacing():
    """Predict time to 100% based on recent burn rates."""
    entries = _load_ledger(last_n=200)
    drains = [e for e in entries if e.get("type") == "tool_drain" and e.get("delta_5h", 0) > 0][-5:]
    if not drains:
        return None
    
    avg_burn = sum(d.get("burn_rate_per_min", 0) for d in drains) / len(drains)
    if avg_burn <= 0:
        return None
    
    five, _, five_reset_ts, _ = _current_pct()
    try:
        remaining = 100 - float(five)
    except Exception:
        return None
    
    if remaining <= 0:
        return {"status": "at_limit", "mins_to_reset": 0, "avg_burn": avg_burn}
    
    mins_to_100 = remaining / avg_burn
    
    try:
        reset = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
        mins_to_reset = max(0, (reset - datetime.now(timezone.utc)).total_seconds() / 60)
    except Exception:
        mins_to_reset = 0
    
    return {
        "status": "pacing",
        "mins_to_100": mins_to_100,
        "mins_to_reset": mins_to_reset,
        "avg_burn": avg_burn,
        "remaining_pct": remaining,
    }


def _get_active_account():
    """Return (label, name, lane) for active account."""
    try:
        d = json.loads((Path.home() / ".claude/accounts.json").read_text())
        active = d.get("active", "?")
        for acct in d.get("accounts", []):
            if acct.get("label") == active:
                return active, acct.get("name", "?"), acct.get("lane", "?")
        return active, "?", "?"
    except Exception:
        return "?", "?", "?"


def make_header(five, seven, five_reset_ts, seven_reset_ts):
    budget = _budget()
    def bar(pct, width=20):
        try:
            pct_f = float(pct)
            filled = int(pct_f * width / 100)
            color = "green" if pct_f < 50 else ("yellow" if pct_f < 75 else "red")
            pct_display = f"{pct_f:.1f}" if pct_f != int(pct_f) else str(int(pct_f))
        except Exception:
            filled, color, pct_display = 0, "dim", "?"
        return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}] {pct_display}%"

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left")
    t.add_column(justify="left")
    t.add_row(
        f"[bold]5h window[/bold]   {bar(five)}",
        f"[bold]7d window[/bold]   {bar(seven)}",
    )
    t.add_row(
        f"resets in [cyan]{_countdown(five_reset_ts)}[/cyan]",
        f"resets [cyan]{_reset_day(seven_reset_ts)}[/cyan]",
    )
    label, name, lane = _get_active_account()
    acct_color = "cyan" if label == "A" else ("magenta" if label == "B" else "yellow")
    t.add_row(
        f"[dim]Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]",
        f"[{acct_color}]Account {label}[/{acct_color}]: {name} [dim]({lane})[/dim]",
    )
    # Token pacing prediction
    pacing = _token_pacing()
    if pacing:
        if pacing["status"] == "at_limit":
            pace_str = f"[red]AT LIMIT[/red] — reset in {_countdown(five_reset_ts)}"
        else:
            m100 = pacing["mins_to_100"]
            mr = pacing["mins_to_reset"]
            burn = pacing["avg_burn"]
            if m100 < mr:
                pace_str = f"[yellow]100% in ~{m100:.0f}m[/yellow] at {burn:.1f}%/min"
            else:
                headroom = mr - m100
                pace_str = f"[green]~{pacing['remaining_pct']:.0f}% left[/green] at {burn:.1f}%/min — resets first"
        t.add_row(pace_str, "")

    return Panel(t, title="[bold white]Token Monitor[/bold white]", border_style="bright_blue")


def make_urgent_panel():
    """Return urgent alerts panel, or None if nothing urgent."""
    five, seven, five_reset_ts, seven_reset_ts = _current_pct()
    
    alerts = []
    
    # Check 5h window — unallocated tokens expiring soon
    try:
        reset = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
        mins_left = int((reset - datetime.now(timezone.utc)).total_seconds() / 60)
        pct_used = float(five)
        pct_remaining = 100 - pct_used
        
        if mins_left <= 30 and pct_remaining >= 1:
            if mins_left <= 5:
                urgency = "[bold red blink]CRITICAL[/bold red blink]"
                color = "red"
            elif mins_left <= 10:
                urgency = "[bold red]URGENT[/bold red]"
                color = "red"
            elif mins_left <= 15:
                urgency = "[bold yellow]WARNING[/bold yellow]"
                color = "yellow"
            else:
                urgency = "[yellow]HEADS UP[/yellow]"
                color = "yellow"
            
            alerts.append(
                f"  {urgency} — [bold]{pct_remaining:.0f}% tokens unused[/bold], "
                f"resets in [{color}]{mins_left}m[/{color}]. Use them or lose them."
            )
    except Exception:
        pass

    # Check for runaway burn rate from drain events — with actionable detail
    try:
        entries = _load_ledger(last_n=200)
        drain_events = [e for e in entries if e.get("type") == "tool_drain" and e.get("delta_5h", 0) > 0][-5:]
        if drain_events:
            last = drain_events[-1]
            burn = float(last.get("burn_rate_per_min", 0))
            num_sessions = int(last.get("cli_sessions", 0))
            delta = float(last.get("delta_5h", 0))

            if burn > 6 or (burn > 3 and num_sessions >= 2):
                # Identify the top burner from active sessions
                active = _active_sessions()
                top_pid = None
                top_delta = 0
                top_directive = "—"
                top_idle_secs = 0
                for item in active:
                    pid, _, directive, delta_str = item[0], item[1], item[2], item[3]
                    try:
                        d = float(delta_str.strip("+%"))
                    except Exception:
                        d = 0
                    if d > top_delta:
                        top_delta = d
                        top_pid = pid
                        top_directive = directive
                        secs, _ = _session_last_activity(pid)
                        top_idle_secs = secs or 0

                severity = "[bold red]RUNAWAY[/bold red]" if burn > 6 else "[yellow]HIGH BURN[/yellow]"
                line1 = (
                    f"  {severity} — {burn:.1f}%/min across "
                    f"{num_sessions} session{'s' if num_sessions != 1 else ''}."
                )
                alerts.append(line1)

                if top_pid:
                    idle_m = top_idle_secs // 60
                    line2 = (
                        f"  Top burner: [bold cyan]cc-{top_pid}[/bold cyan] "
                        f"at [bold]+{top_delta:.0f}%[/bold] "
                        f"({top_directive[:25]})"
                    )
                    if top_idle_secs > 300:
                        line2 += (
                            f" — [bold red]idle {idle_m}m[/bold red]. "
                            f"Likely stuck. Run: [bold]kill {top_pid}[/bold]"
                        )
                    elif top_idle_secs > 60:
                        line2 += f" — idle {idle_m}m. Monitor or close if unneeded."
                    else:
                        line2 += " — actively working."
                    alerts.append(line2)
    except Exception:
        pass

    if not alerts:
        return None

    from rich.text import Text
    t = Table(box=None, padding=(0, 0), expand=True, show_header=False)
    t.add_column(ratio=1)
    for alert in alerts:
        t.add_row(alert)
    
    return Panel(t, title="[bold red]⚠ URGENT[/bold red]", border_style="red")


def _etime_to_secs(etime):
    """Parse ps etime string ([[DD-]HH:]MM:SS) to total seconds."""
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = etime.split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        else:
            return None
        return days * 86400 + h * 3600 + m * 60 + s
    except Exception:
        return None


def make_sessions_panel():
    """Active Sessions with inline call detail sub-rows."""
    sessions = _active_sessions()
    entries = _load_ledger(last_n=500)
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()

    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1), expand=True)
    t.add_column("When", width=9, no_wrap=True)
    t.add_column("Session", width=10, no_wrap=True)
    t.add_column("Src", width=10, no_wrap=True)
    t.add_column("Mdl", width=10, no_wrap=True)
    t.add_column("Dur", width=12, no_wrap=True)
    t.add_column("Used", width=11, no_wrap=True)
    t.add_column("Directive", overflow="ellipsis", no_wrap=True)

    if not sessions:
        t.add_row("", "[dim]—[/dim]", "", "", "", "", "[dim]no active sessions[/dim]")
        return Panel(t, title="[bold]Active Sessions[/bold]  [dim](live)[/dim]", border_style="cyan")

    # Single-pass ledger scan: build model, last call, first output per session
    model_map = {}    # type: Dict[str, str]
    last_call = {}    # type: Dict[str, Tuple[datetime, str, int]]
    first_out = {}    # type: Dict[str, int]
    for e in entries:
        sid = e.get("session", "")
        if not sid:
            continue
        mdl = e.get("model")
        if mdl and mdl != "?":
            model_map[sid] = mdl
        if e.get("type") == "tool_use":
            if sid not in first_out:
                first_out[sid] = e.get("output_tokens", 0)
            try:
                ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                tool = _shorten_tool(e.get("tool", "?"))
                out = e.get("output_tokens", 0)
                last_call[sid] = (ts, tool, out)
            except Exception:
                pass

    for item in sessions:
        pid, age, directive, delta = item[0], item[1], item[2], item[3]
        source = item[4] if len(item) > 4 else "?"
        sid = f"cc-{pid}"

        # ── Header row ──
        elapsed_s = _etime_to_secs(age)
        start_str = (now_local - timedelta(seconds=elapsed_s)).strftime("%H:%M:%S") if elapsed_s else "?"

        color = "green"
        if delta == "new":
            color = "dim"
        else:
            try:
                val = float(delta.strip("+%"))
                color = "red" if val > 10 else ("yellow" if val > 5 else "green")
            except Exception:
                pass

        mdl = _abbrev_model(model_map.get(sid, "?"))
        mdl_style = "magenta" if "opus" in mdl else ("cyan" if "sonnet" in mdl else "dim")
        src_color = "yellow" if ("/" in source or source == "paperclip") else ("green" if source == "cli" else ("cyan" if "atlas" in source else "dim"))

        t.add_row(
            f"[dim]{start_str}[/dim]",
            f"[cyan]{sid}[/cyan]",
            f"[{src_color}]{source}[/{src_color}]",
            f"[{mdl_style}]{mdl}[/{mdl_style}]",
            f"[dim]{age}[/dim]",
            f"[{color}]{delta}[/{color}]",
            directive,
        )

        # ── Sub-row: live call state ──
        cpu = _get_pid_cpu(pid)
        lc = last_call.get(sid)
        if lc:
            secs_since = int((now_utc - lc[0]).total_seconds())
            tool_name = lc[1]
            token_delta = lc[2] - first_out.get(sid, 0)
        else:
            secs_since = None
            tool_name = "?"
            token_delta = 0

        # State detection
        if secs_since is not None and secs_since < 15:
            state = f"[bold green]>> {tool_name[:12]}[/bold green]"
        elif cpu > 20:
            state = "[bold yellow]thinking...[/bold yellow]"
        elif secs_since is not None and secs_since < 120:
            state = f"[dim]~ {tool_name[:12]}[/dim]"
        else:
            state = "[dim]idle[/dim]"

        # Elapsed
        if secs_since is not None:
            m, s = divmod(secs_since, 60)
            elapsed_str = f"{m}m{s:02d}s" if m else f"{s}s"
        else:
            elapsed_str = "—"

        # Tokens
        tok_str = f"{token_delta / 1000:.1f}k" if token_delta >= 1000 else str(token_delta)

        # CPU
        cpu_str = f"{cpu:.0f}%"
        cpu_style = "bold yellow" if cpu > 50 else ("dim" if cpu < 5 else "")
        cpu_val = f"[{cpu_style}]{cpu_str}[/{cpu_style}]" if cpu_style else cpu_str

        t.add_row(
            "", "", "",
            f"  {state}",
            f"[dim]ago:[/dim] {elapsed_str}",
            f"[dim]tok:[/dim] {tok_str}",
            f"[dim]cpu:[/dim] {cpu_val}",
        )


    return Panel(t, title="[bold]Active Sessions[/bold]  [dim](live)[/dim]", border_style="cyan")


def _get_pid_cpu(pid):
    """Get CPU usage percentage for a PID."""
    try:
        r = subprocess.run(
            ['ps', '-p', str(pid), '-o', '%cpu='],
            capture_output=True, text=True, timeout=2,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def make_tool_stats():
    entries = _load_ledger(last_n=500)
    tool_events = [e for e in entries if e.get("type") == "tool_use"]
    counts = defaultdict(int)
    for e in tool_events:
        counts[e.get("tool", "unknown")] += 1
    t = Table(show_header=True, header_style="bold green", box=None, padding=(0, 1), expand=True)
    t.add_column("Tool", overflow="ellipsis", no_wrap=True, ratio=4)
    t.add_column("Calls", min_width=5, justify="right", no_wrap=True)
    for tool, count in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:12]:
        t.add_row(_shorten_tool(tool), str(count))
    return Panel(t, title="[bold]Tool Frequency[/bold]  [dim](last 500 events)[/dim]", border_style="green")


def make_drain_panel():
    entries = _load_ledger(last_n=200)
    drain_events = [e for e in entries if e.get("type") == "tool_drain" and e.get("delta_5h", 0) > 0][-12:]
    status_color, status_msg = _drain_status(drain_events)

    t = Table(show_header=True, header_style="bold yellow", box=None, padding=(0, 1), expand=True)
    t.add_column("Time", min_width=8, no_wrap=True)
    t.add_column("Delta", min_width=6, no_wrap=True)
    t.add_column("Burn/min", min_width=8, no_wrap=True)
    t.add_column("Sessions", min_width=4, no_wrap=True)
    t.add_column("Desktop", min_width=7, no_wrap=True)

    t.add_row(f"[{status_color}]{status_msg}[/{status_color}]", "", "", "", "")

    if not drain_events:
        t.add_row("[dim green]no drain events recorded[/dim green]", "", "", "", "")
    else:
        for e in reversed(drain_events):
            ts = e.get("ts", "")
            try:
                ts_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
            except Exception:
                ts_str = "?"
            delta = e.get("delta_5h", 0)
            burn = e.get("burn_rate_per_min", 0)
            sessions = e.get("cli_sessions", "?")
            desktop = "YES" if e.get("desktop") else "no"
            burn_color = "red" if float(burn) > 1 else "yellow"
            t.add_row(
                f"[dim]{ts_str}[/dim]", f"[red]+{delta}%[/red]",
                f"[{burn_color}]{burn:.2f}%[/{burn_color}]", str(sessions),
                f"[bold red]{desktop}[/bold red]" if desktop == "YES" else f"[dim]{desktop}[/dim]",
            )
    return Panel(t, title="[bold]Passive Drain[/bold]  [dim](non-zero only)[/dim]", border_style="yellow")
