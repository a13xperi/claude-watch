#!/usr/bin/env python3
"""
claude-watch — real-time terminal dashboard for Claude Code token activity.
Shows every tool call, active sessions, burn rate, and passive drain.
"""

import json
import os
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

LEDGER = Path.home() / ".claude/logs/token-ledger.jsonl"
BUDGET_FILE = Path.home() / ".claude/token-budget.json"
TRANSCRIPTS_DIR = Path.home() / ".claude/projects/-Users-a13xperi"
SESSION_INDEX = Path.home() / ".claude/logs/session-index.jsonl"

console = Console()

# ── helpers ───────────────────────────────────────────────────────────────────

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
    """Format 7d reset as 'Mon Apr 13'."""
    if not reset_ts:
        return "?"
    try:
        dt = datetime.fromisoformat(reset_ts.replace("Z", "+00:00")).astimezone()
        day = dt.day
        return dt.strftime(f"%a %b {day}")
    except Exception:
        return "?"

def _abbrev_model(model: str) -> str:
    """Shorten model ID to display name."""
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

def _active_sessions():
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
                    start_pct = float(Path(f"/tmp/claude-token-state-{pid}").read_text().split()[0])
                    cur = float(_current_pct()[0])
                    delta = f"+{round(cur - start_pct, 1)}%"
                except Exception:
                    pass
                sessions.append((pid, etime, directive or "—", delta))
    except Exception:
        pass
    return sessions

def _budget():
    try:
        if BUDGET_FILE.exists():
            return json.loads(BUDGET_FILE.read_text()).get("per_session_pct", 15)
    except Exception:
        pass
    return 15

# ── ledger ────────────────────────────────────────────────────────────────────

_ledger_cache_time = 0.0
_ledger_cache: list = []

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

# ── session index ─────────────────────────────────────────────────────────────

_index_cache: dict = {}   # session_id -> entry dict
_index_loaded = False
_index_building = False
_index_thread = None  # type: threading.Thread | None

def _load_index():
    global _index_cache, _index_loaded
    cache: dict = {}
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

def _parse_transcript(f: Path):
    """Parse a single transcript file. Returns session dict or None."""
    total_out = 0
    first_ts = last_ts = None
    slug = last_prompt = None
    model_counts: dict = defaultdict(int)
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
                        model_counts[mdl] += out  # weight by output tokens
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
    return {
        "session_id": f.stem,
        "first_ts": first_ts.isoformat(),
        "last_ts": (last_ts or first_ts).isoformat(),
        "output_tokens": total_out,
        "slug": slug or "",
        "directive": directive,
        "model": dominant_model,
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
        for f in TRANSCRIPTS_DIR.glob("*.jsonl"):
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
    current_session_id = None
    try:
        d = json.loads(Path("/tmp/statusline-debug.json").read_text())
        current_session_id = d.get("session_id", "")
    except Exception:
        pass

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

        # Duration
        secs = int((last_ts - first_ts).total_seconds())
        h, r = divmod(secs, 3600)
        m = r // 60
        dur_str = f"{h}h{m:02d}m" if h else f"{m}m"

        # pct estimate — today only (ledger only has today's data)
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
            "date": session_date,
        })

    sessions.sort(key=lambda s: s["last_ts"], reverse=True)
    return sessions

# ── panels ────────────────────────────────────────────────────────────────────

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

    countdown = _countdown(five_reset_ts)
    seven_day_str = _reset_day(seven_reset_ts)

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left")
    t.add_column(justify="left")
    t.add_row(
        f"[bold]5h window[/bold]   {bar(five)}   resets in [cyan]{countdown}[/cyan]",
        f"[bold]7d window[/bold]   {bar(seven)}   resets [cyan]{seven_day_str}[/cyan]",
    )
    t.add_row(
        f"[dim]Budget: {budget}% per session[/dim]",
        f"[dim]Updated: {datetime.now().strftime('%H:%M:%S')}[/dim]",
    )
    return Panel(t, title="[bold white]Token Monitor[/bold white]", border_style="bright_blue")

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

def make_sessions_panel():
    sessions = _active_sessions()
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1), expand=True)
    t.add_column("PID", min_width=6, no_wrap=True)
    t.add_column("Age", min_width=6, no_wrap=True)
    t.add_column("Used", min_width=6, no_wrap=True)
    t.add_column("Status", min_width=14, no_wrap=True, overflow="ellipsis")
    t.add_column("Directive", overflow="ellipsis", no_wrap=True)
    if not sessions:
        t.add_row("[dim]—[/dim]", "", "", "", "[dim]no active sessions[/dim]")
    else:
        for pid, age, directive, delta in sessions:
            color = "green"
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
            t.add_row(
                f"[cyan]{pid}[/cyan]",
                f"[dim]{age}[/dim]",
                f"[{color}]{delta}[/{color}]",
                status,
                directive,
            )
    return Panel(t, title="[bold]Active Sessions[/bold]", border_style="cyan")

def make_session_history_panel():
    sessions = _get_session_history()

    t = Table(show_header=True, header_style="bold blue", box=None, padding=(0, 1), expand=True)
    t.add_column("Time", min_width=5, no_wrap=True)
    t.add_column("Dur", min_width=5, no_wrap=True)
    t.add_column("Model", min_width=6, no_wrap=True)
    t.add_column("~5h%", min_width=6, no_wrap=True)
    t.add_column("OutTok", min_width=7, no_wrap=True, justify="right")
    t.add_column("Directive / last prompt", overflow="ellipsis", no_wrap=True)

    if not sessions:
        msg = "[dim]building index...[/dim]" if _index_building else "[dim]no sessions found[/dim]"
        t.add_row(msg, "", "", "", "", "")
        title = "[bold]Session History[/bold]  [dim](indexing...)[/dim]" if _index_building else "[bold]Session History[/bold]"
        return Panel(t, title=title, border_style="blue")

    today = datetime.now(timezone.utc).astimezone().date()
    yesterday = today - timedelta(days=1)
    current_group = None
    shown = 0
    MAX = 25

    for s in sessions:
        if shown >= MAX:
            break
        date = s["date"]
        group = "Today" if date == today else ("Yesterday" if date == yesterday else date.strftime("%b %-d"))

        if group != current_group:
            if current_group is not None:
                t.add_row("", "", "", "", "", "")
            sep = f"── {group} " + "─" * max(0, 34 - len(group))
            t.add_row(f"[dim]{sep}[/dim]", "", "", "", "", "")
            current_group = group

        end_str = s["last_ts"].astimezone().strftime("%H:%M")
        pct_str = s["pct_str"]
        pct_color = "dim"
        if pct_str != "—":
            try:
                v = float(pct_str.strip("+%"))
                pct_color = "red" if v > 10 else ("yellow" if v > 5 else "green")
            except Exception:
                pass
        out_k = s["output_tokens"]
        out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)
        directive = (s["directive"] or "—")[:32]

        mdl = _abbrev_model(s.get("model", ""))
        mdl_color = "magenta" if mdl == "opus" else ("cyan" if mdl == "sonnet" else "dim")
        t.add_row(
            f"[dim]{end_str}[/dim]",
            f"[dim]{s['dur_str']}[/dim]",
            f"[{mdl_color}]{mdl}[/{mdl_color}]",
            f"[{pct_color}]{pct_str}[/{pct_color}]",
            f"[dim]{out_str}[/dim]",
            directive,
        )
        shown += 1

    total = len(sessions)
    extra = f"  [dim](showing {MAX} of {total})[/dim]" if total > MAX else (
        "  [dim](indexing...)[/dim]" if _index_building else ""
    )
    return Panel(t, title=f"[bold]Session History[/bold]{extra}", border_style="blue")

def make_live_feed(last_n=18):
    entries = _load_ledger(last_n=100)
    tool_events = [e for e in entries if e.get("type") == "tool_use"][-last_n:]

    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 1), expand=True)
    t.add_column("Time", min_width=8, no_wrap=True)
    t.add_column("Session", min_width=9, no_wrap=True)
    t.add_column("Directive", min_width=12, max_width=16, overflow="ellipsis", no_wrap=True)
    t.add_column("Tool", overflow="ellipsis", no_wrap=True, ratio=3)
    t.add_column("Δ5h%", min_width=6, no_wrap=True)

    if not tool_events:
        t.add_row("[dim]—[/dim]", "", "", "[dim]no events yet[/dim]", "")
    else:
        prev_pct: dict = {}
        for e in tool_events:
            sess = e.get("session", "?")
            pct = e.get("five_pct")
            if pct is not None and sess not in prev_pct:
                prev_pct[sess] = pct

        for e in reversed(tool_events):
            ts = e.get("ts", "")
            try:
                ts_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
            except Exception:
                ts_str = ts[-8:] if ts else "?"

            tool = e.get("tool", "?")
            if tool.startswith("mcp__claude_ai_"):
                tool = "mcp:" + tool.replace("mcp__claude_ai_", "").replace("__", "/")
            elif tool.startswith("mcp__"):
                tool = "mcp:" + tool[5:]

            session = e.get("session", "?")
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
                color = "red" if tick >= 2 else "yellow"
                delta_str = f"[bold {color}]▲+{tick:.0f}%[/bold {color}]"
            elif cumulative:
                try:
                    c = float(cumulative)
                    delta_str = f"[dim]+{c:.1f}%[/dim]" if c > 0 else "[dim]—[/dim]"
                except Exception:
                    delta_str = "[dim]—[/dim]"
            else:
                delta_str = "[dim]—[/dim]"

            t.add_row(
                f"[dim]{ts_str}[/dim]",
                f"[cyan]{session}[/cyan]",
                f"[dim]{directive[:14]}[/dim]",
                tool,
                delta_str,
            )

    return Panel(t, title="[bold]Tool Call Feed[/bold]  [dim](newest first)[/dim]", border_style="magenta")

def _drain_status(drain_events):
    """Return (color, message) status line for passive drain panel."""
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
        return "red", f"✖  Spike — +{delta:.0f}% in one interval with no tool calls. Check for runaway."
    if burn > 6:
        return "red", f"✖  Runaway — {burn:.1f}%/min burn rate detected"
    if sessions > 2:
        per = burn / sessions if sessions else burn
        return "yellow", f"▲  {sessions} sessions open — baseline ~{per:.1f}%/min each. Close unused sessions."
    return "green", f"●  Normal — {sessions} session{'s' if sessions != 1 else ''}, ~{burn:.0f}%/min baseline"

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

    # Status line as first row
    t.add_row(f"[{status_color}]{status_msg}[/{status_color}]", "", "", "", "")

    if not drain_events:
        t.add_row("[dim green]no drain events recorded[/dim green]", "", "", "", "")
    else:
        for e in reversed(drain_events):
            ts = e.get("ts", "")
            try:
                ts_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
            except Exception:
                ts_str = ts[-8:] if ts else "?"
            delta = e.get("delta_5h", 0)
            burn = e.get("burn_rate_per_min", 0)
            sessions = e.get("cli_sessions", "?")
            desktop = "YES" if e.get("desktop") else "no"
            try:
                burn_color = "red" if float(burn) > 1 else "yellow"
            except Exception:
                burn_color = "yellow"
            t.add_row(
                f"[dim]{ts_str}[/dim]",
                f"[red]+{delta}%[/red]",
                f"[{burn_color}]{burn:.2f}%[/{burn_color}]",
                str(sessions),
                f"[bold red]{desktop}[/bold red]" if desktop == "YES" else f"[dim]{desktop}[/dim]",
            )

    return Panel(
        t,
        title="[bold]Passive Drain[/bold]  [dim](tokens burning between tool calls — non-zero only)[/dim]",
        border_style="yellow",
    )

def make_tool_stats():
    entries = _load_ledger(last_n=500)
    tool_events = [e for e in entries if e.get("type") == "tool_use"]
    counts: dict = defaultdict(int)
    for e in tool_events:
        tool = e.get("tool", "unknown")
        counts[tool] += 1
    t = Table(show_header=True, header_style="bold green", box=None, padding=(0, 1), expand=True)
    t.add_column("Tool", overflow="ellipsis", no_wrap=True, ratio=4)
    t.add_column("Calls", min_width=5, justify="right", no_wrap=True)
    for tool, count in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:12]:
        display = tool
        if tool.startswith("mcp__claude_ai_"):
            display = "mcp:" + tool.replace("mcp__claude_ai_", "").replace("__", "/")
        elif tool.startswith("mcp__"):
            display = "mcp:" + tool[5:]
        t.add_row(display, str(count))
    return Panel(t, title="[bold]Tool Frequency[/bold]  [dim](last 500 events)[/dim]", border_style="green")

# ── main loop ─────────────────────────────────────────────────────────────────

def build_layout(five, seven, five_reset_ts, seven_reset_ts):
    layout = Layout()
    layout.split_column(
        Layout(make_header(five, seven, five_reset_ts, seven_reset_ts), size=5),
        Layout(name="top", ratio=2),
        Layout(make_session_history_panel(), ratio=3),
        Layout(name="feed", ratio=3),
        Layout(make_drain_panel(), ratio=2),
    )
    layout["top"].split_row(
        Layout(make_sessions_panel(), ratio=1),
        Layout(make_tool_stats(), ratio=1),
    )
    layout["feed"].split_row(
        Layout(make_live_feed(), ratio=1),
    )
    return layout

def main():
    # Kick off index build immediately on startup
    _ensure_index()
    console.print("[bold bright_blue]claude-watch[/bold bright_blue] starting... (Ctrl+C to exit)\n")
    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            five, seven, five_reset_ts, seven_reset_ts = _current_pct()
            live.update(build_layout(five, seven, five_reset_ts, seven_reset_ts))
            time.sleep(0.5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]claude-watch exited.[/dim]")
