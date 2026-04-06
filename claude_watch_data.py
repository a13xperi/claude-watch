"""
claude-watch data layer — shared by Rich and Textual versions.
All data fetching, caching, and computation lives here.
"""

import json
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
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return model[:8]


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
                    if age_secs < 120 and raw_delta > 5:
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


def _load_ledger(last_n=500):
    global _ledger_cache_time, _ledger_cache
    if not LEDGER.exists():
        return []
    mtime = LEDGER.stat().st_mtime
    if mtime == _ledger_cache_time:
        return _ledger_cache
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
    _ledger_cache = entries[-last_n:]
    _ledger_cache_time = mtime
    return _ledger_cache


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
    if "paperclip" in parent:
        source = "paperclip"
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
            with open(SESSION_INDEX, "a") as fh:
                for entry in new_entries:
                    fh.write(json.dumps(entry) + "\n")
            _index_cache.update({e["session_id"]: e for e in new_entries})
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
            m = r // 60
            dur_str = f"{h}h{m:02d}m" if h else f"{m}m"

        pct_str = "—"
        if session_date == today:
            ps = _interpolate_five_pct(first_ts)
            pe = _interpolate_five_pct(last_ts)
            if ps is not None and pe is not None:
                try:
                    d_pct = round(float(pe) - float(ps), 1)
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

        rows.append({
            "ts_str": ts_str,
            "session": session,
            "tool": tool,
            "directive": directive,
            "delta_str": delta_str,
            "delta_style": delta_style,
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

def make_header(five, seven, five_reset_ts, seven_reset_ts):
    budget = _budget()
    def bar(pct, width=20):
        try:
            pct_f = float(pct)
            filled = int(pct_f * width / 100)
            color = "green" if pct_f < 50 else ("yellow" if pct_f < 75 else "red")
        except Exception:
            filled, color = 0, "dim"
        return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}] {pct}%"

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
    t.add_row(
        f"[dim]Budget: {budget}% per session[/dim]",
        f"[dim]Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]",
    )
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
    
    if not alerts:
        return None
    
    from rich.text import Text
    t = Table(box=None, padding=(0, 0), expand=True, show_header=False)
    t.add_column(ratio=1)
    for alert in alerts:
        t.add_row(alert)
    
    return Panel(t, title="[bold red]⚠ URGENT[/bold red]", border_style="red")


def make_sessions_panel():
    sessions = _active_sessions()
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1), expand=True)
    t.add_column("PID", min_width=6, no_wrap=True)
    t.add_column("Age", min_width=6, no_wrap=True)
    t.add_column("Used", min_width=6, no_wrap=True)
    t.add_column("Status", min_width=14, no_wrap=True, overflow="ellipsis")
    t.add_column("Source", width=10, no_wrap=True)
    t.add_column("Directive", overflow="ellipsis", no_wrap=True)
    if not sessions:
        t.add_row("[dim]—[/dim]", "", "", "", "", "[dim]no active sessions[/dim]")
    else:
        for item in sessions:
            pid, age, directive, delta = item[0], item[1], item[2], item[3]
            source = item[4] if len(item) > 4 else "?"
            color = "green"
            if delta == "new":
                color = "dim"
            else:
                try:
                    val = float(delta.strip("+%"))
                    color = "red" if val > 10 else ("yellow" if val > 5 else "green")
                except Exception:
                    pass
            secs_ago, last_tool = _session_last_activity(pid)
            if secs_ago is not None and secs_ago < 45:
                status = f"[bold green]⚡ {last_tool[:10]}[/bold green]"
            elif secs_ago is not None and secs_ago < 300:
                m = secs_ago // 60
                s = secs_ago % 60
                ago = f"{m}m{s:02d}s" if m else f"{s}s"
                status = f"[dim]↺ {last_tool[:8]} {ago}[/dim]"
            else:
                status = "[dim]● idle[/dim]"
            src_color = "yellow" if source == "paperclip" else ("green" if source == "cli" else "dim")
            t.add_row(
                f"[cyan]{pid}[/cyan]", f"[dim]{age}[/dim]",
                f"[{color}]{delta}[/{color}]", status,
                f"[{src_color}]{source}[/{src_color}]", directive,
            )
    return Panel(t, title="[bold]Active Sessions[/bold]", border_style="cyan")


def make_active_calls_panel():
    """Show the most recent tool calls for each active session."""
    sessions = _active_sessions()
    entries = _load_ledger(last_n=200)

    t = Table(show_header=True, header_style="bold white", box=None, padding=(0, 1), expand=True)
    t.add_column("Session", width=10, no_wrap=True)
    t.add_column("Tool", width=10, no_wrap=True)
    t.add_column("Ago", width=5, no_wrap=True)
    t.add_column("Source", width=10, no_wrap=True)
    t.add_column("Directive", overflow="ellipsis", no_wrap=True, ratio=1)

    if not sessions:
        t.add_row("[dim]—[/dim]", "", "", "", "[dim]no active sessions[/dim]")
        return Panel(t, title="[bold]Active Calls[/bold]", border_style="bright_white")

    now = datetime.now(timezone.utc)
    active_pids = {item[0] for item in sessions}

    for item in sessions:
        pid = item[0]
        directive = item[2]
        source = item[4] if len(item) > 4 else "?"
        sid = f"cc-{pid}"

        # Get last 3 tool calls for this session
        session_calls = [
            e for e in reversed(entries)
            if e.get("session") == sid and e.get("type") == "tool_use"
        ][:3]

        if not session_calls:
            src_color = "yellow" if source == "paperclip" else ("green" if source == "cli" else "dim")
            t.add_row(
                f"[cyan]{sid}[/cyan]", "[dim]—[/dim]", "", f"[{src_color}]{source}[/{src_color}]",
                f"[dim]{directive[:30]}[/dim]",
            )
            continue

        for i, e in enumerate(session_calls):
            tool = _shorten_tool(e.get("tool", "?"))
            try:
                ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                secs = int((now - ts).total_seconds())
                ago = f"{secs}s" if secs < 60 else f"{secs//60}m"
            except Exception:
                ago = "?"

            if i == 0:
                src_color = "yellow" if source == "paperclip" else ("green" if source == "cli" else "dim")
                t.add_row(
                    f"[cyan]{sid}[/cyan]",
                    f"[bold green]{tool}[/bold green]" if secs < 45 else tool,
                    f"[dim]{ago}[/dim]",
                    f"[{src_color}]{source}[/{src_color}]",
                    f"{directive[:30]}",
                )
            else:
                t.add_row(
                    "", f"[dim]{tool}[/dim]", f"[dim]{ago}[/dim]", "", "",
                )

    return Panel(t, title="[bold]Active Calls[/bold]", border_style="bright_white")


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
