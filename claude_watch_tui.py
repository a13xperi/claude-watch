#!/usr/bin/env python3
"""
claude-watch TUI — Textual-based interactive dashboard for Claude Code token monitoring.
Scrollable panels, keyboard navigation, no dead space.
"""

import json
import math
import os
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, DataTable, Static

from rich.panel import Panel
from rich.table import Table as RichTable

from claude_watch_data import (
    make_urgent_panel,
    _abbrev_model,
    _active_pids,
    _active_sessions,
    _build_or_update_index,
    _build_pid_map,
    _countdown,
    _current_pct,
    _etime_to_secs,
    _estimate_cost,
    _extract_accomplishments,
    _format_cost,
    _get_agent_stats,
    _get_burndown_data,
    _get_token_attribution,
    _get_call_data_map,
    _get_call_history,
    _get_daily_usage,
    _get_mcp_stats,
    _get_peer_sessions,
    _get_pid_cpu,
    _get_session_history,
    _get_session_turns,
    _get_system_health,
    _get_usage_metrics,
    _gravity_center,
    _index_building,
    _index_cache,
    _index_lock,
    _load_index,
    _load_ledger,
    _shorten_tool,
    check_and_notify,
    export_session_history_csv,
    focus_session_terminal,
    get_account_capacity_display,
    lookup_by_ccid,
    make_drain_panel,
    make_header,
    make_sessions_panel,
    make_skills_panel,
    make_tool_stats,
)

class LazyView(ScrollableContainer):
    """Content view that lazy-loads data on first display."""
    _loaded: bool = False

    def load_content(self) -> None:
        """Override to populate widgets. Called once on first show."""
        pass

    def refresh_content(self) -> None:
        """Override for timer-driven refresh when visible."""
        pass


def _start_hot_reload_watcher(app):
    # type: (Any) -> None
    """Watch source files for changes. Signal the app instead of auto-restarting."""
    watch_dir = Path(__file__).resolve().parent

    def _snapshot():
        # type: () -> Dict[Path, float]
        result = {}
        for p in watch_dir.glob("*.py"):
            try:
                result[p] = p.stat().st_mtime
            except Exception:
                pass
        tcss = watch_dir / "claude_watch_tui.tcss"
        try:
            result[tcss] = tcss.stat().st_mtime
        except Exception:
            pass
        return result

    mtimes = _snapshot()
    while True:
        time.sleep(2)
        current = _snapshot()
        if current != mtimes:
            mtimes = current
            app.call_from_thread(app._signal_files_changed)


_BACKUP_DIR = Path(f"/tmp/claude-watch-backup-{os.getpid()}")
_SOURCE_DIR = Path(__file__).resolve().parent
_BACKUP_FILES = ["claude_watch_tui.py", "claude_watch_data.py", "claude_watch.py", "claude_watch_tui.tcss"]


def _backup_working_files():
    """Snapshot current source files as last-known-good backup."""
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        import shutil
        for fname in _BACKUP_FILES:
            src = _SOURCE_DIR / fname
            if src.exists():
                shutil.copy2(str(src), str(_BACKUP_DIR / fname))
    except Exception:
        pass


def _restore_backup_files():
    """Restore backed-up files over current files. Returns True if restored."""
    import shutil
    if not _BACKUP_DIR.exists():
        return False
    restored = False
    for fname in _BACKUP_FILES:
        bak = _BACKUP_DIR / fname
        dst = _SOURCE_DIR / fname
        if bak.exists():
            shutil.copy2(str(bak), str(dst))
            restored = True
    return restored


def _project_to_company(project: str) -> tuple[str, str]:
    """Return (company_name, style) from a project string."""
    p = (project or "").lower().strip()
    if p in ("atlas", "atlas-be", "atlas-fe"):
        return "Delphi", "blue"
    if p in ("kaa",):
        return "KAA", "green"
    if p in ("frank",):
        return "Frank", "magenta"
    if p in ("openclaw", "paperclip", "claude-watch"):
        return "Personal", "dim"
    return "—", "dim"


# ── Static widgets (wrap existing Rich renderables) ──────────────────────────


class UrgentAlerts(Static):
    def update_content(self):
        panel = make_urgent_panel()
        if panel:
            self.update(panel)
            self.display = True
        else:
            self.update("")
            self.display = False


class TokenHeader(Static):
    def update_content(self, five, seven, fr, sr):
        self.update(make_header(five, seven, fr, sr))


class AccountCapacityPanel(Static):
    """Compact side-by-side view of all Claude accounts."""

    def update_content(self):
        from claude_watch_data import _get_all_account_capacities
        accounts = _get_all_account_capacities()
        if not accounts:
            self.update("")
            self.display = False
            return

        def mini_bar(pct_str, width=6):
            try:
                pct = float(pct_str)
                filled = int(pct * width / 100)
                color = "green" if pct < 50 else ("yellow" if pct < 75 else "red")
                return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}] {pct:.0f}%"
            except Exception:
                return f"[dim]{'░' * width}[/dim] —"

        t = RichTable(show_header=False, box=None, padding=(0, 2), expand=True)
        for _ in accounts:
            t.add_column(justify="left")

        # Row 1: Account labels
        labels = []
        for a in accounts:
            color = "cyan" if a["label"] == "A" else ("magenta" if a["label"] == "B" else "yellow")
            active = " ← ACTIVE" if a["active"] else ""
            labels.append(f"[{color} bold]Account {a['label']}[/{color} bold] [dim]({a['name']})[/dim]{active}")
        t.add_row(*labels)

        # Row 2: 5h bars
        t.add_row(*[f"5h: {mini_bar(a['five_pct'])}" for a in accounts])

        # Row 3: 7d bars
        t.add_row(*[f"7d: {mini_bar(a['seven_pct'])}" for a in accounts])

        self.update(Panel(t, title="[bold]Account Capacity[/bold]", border_style="dim"))


class ActiveSessionsTable(DataTable):
    """Interactive active sessions table — Enter/f to focus the terminal."""

    BORDER_TITLE = "Active Sessions (live)"
    BORDER_SUBTITLE = "Enter/f to focus terminal"

    BINDINGS = [
        Binding("f", "focus_selected", "Focus terminal", show=True),
    ]

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.add_column("When", width=9, key="when")
        self.add_column("Session", width=10, key="session")
        self.add_column("Src", width=10, key="src")
        self.add_column("Co", width=8, key="co")
        self.add_column("Project", width=12, key="project")
        self.add_column("Mdl", width=10, key="mdl")
        self.add_column("Dur", width=12, key="dur")
        self.add_column("Used", width=11, key="used")
        self.add_column("Directive", key="directive")

    def refresh_rows(self):
        """Rebuild the table from live session data + Supabase peers."""
        from claude_watch_data import _detect_source

        sessions = _active_sessions()
        entries = _load_ledger(last_n=500)
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now()

        # Collect local session IDs for dedup against peers
        local_sids = set()  # type: set
        for item in sessions:
            local_sids.add("cc-{}".format(item[0]))

        # Fetch peer sessions from Supabase, excluding local matches
        peers = _get_peer_sessions()
        remote_peers = [p for p in peers if p.get("session_id", "") not in local_sids]

        n_local = len(sessions)
        n_peers = len(remote_peers)
        n_total = n_local + n_peers
        if n_total:
            if n_peers:
                self.border_title = "Active Sessions (live) — {} ({} local, {} peers)".format(
                    n_total, n_local, n_peers
                )
            else:
                self.border_title = "Active Sessions (live) — {}".format(n_total)
        else:
            self.border_title = "Active Sessions (live)"

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        if not sessions and not remote_peers:
            self.add_row(
                "", Text("--", style="dim"), "", "", "", "", "", "",
                Text("no active sessions", style="dim"),
                key="empty",
            )
            return

        # Single-pass ledger scan: build model, last call, first output per session
        model_map = {}    # type: dict
        last_call = {}    # type: dict
        first_out = {}    # type: dict
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

            # Header row
            elapsed_s = _etime_to_secs(age)
            start_str = (
                (now_local - timedelta(seconds=elapsed_s)).strftime("%H:%M:%S")
                if elapsed_s else "?"
            )

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
            src_color = (
                "yellow" if ("/" in source or source == "paperclip")
                else ("green" if source == "cli"
                       else ("cyan" if "atlas" in source else "dim"))
            )

            # Derive project
            project = "\u2014"
            if source in ("atlas-be", "atlas-fe"):
                project = "atlas"
            elif source == "openclaw":
                project = "openclaw"
            elif source == "frank":
                project = "frank"
            elif "/" in source:
                parts = source.split("/", 1)
                project = parts[1] if len(parts) > 1 else parts[0]
            else:
                d_lower = directive.lower() if directive else ""
                for p in ("claude-watch", "atlas", "paperclip", "openclaw", "frank"):
                    if p in d_lower:
                        project = p
                        break

            if "/" in source:
                co_name = source.split("/", 1)[0]
                co_style = "yellow"
            else:
                co_name, co_style = _project_to_company(project)

            # Compute state BEFORE main row so dot color reflects live activity
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
                state_txt = Text(f">> {tool_name[:12]}", style="bold green")
                dot_color = "bold green"
            elif cpu > 20:
                state_txt = Text("thinking...", style="bold yellow")
                dot_color = "bold yellow"
            elif secs_since is not None and secs_since < 120:
                state_txt = Text(f"~ {tool_name[:12]}", style="dim")
                dot_color = "green"
            else:
                state_txt = Text("idle", style="dim")
                dot_color = "green"

            self.add_row(
                Text(start_str, style="dim"),
                Text.from_markup(f"[{dot_color}]\u25cf [/{dot_color}][cyan]{sid}[/cyan]"),
                Text(source, style=src_color),
                Text(co_name, style=co_style),
                Text(project, style="dim"),
                Text(mdl, style=mdl_style),
                Text(age, style="dim"),
                Text(delta, style=color),
                Text(directive),
                key=f"active-{pid}",
            )

            # Elapsed
            if secs_since is not None:
                m, s = divmod(secs_since, 60)
                elapsed_str = f"{m}m{s:02d}s" if m else f"{s}s"
            else:
                elapsed_str = "\u2014"

            # Tokens
            tok_str = (
                f"{token_delta / 1000:.1f}k" if token_delta >= 1000
                else str(token_delta)
            )

            # CPU
            cpu_str = f"{cpu:.0f}%"
            cpu_style = "bold yellow" if cpu > 50 else ("dim" if cpu < 5 else "")

            self.add_row(
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                state_txt,
                Text(f"ago: {elapsed_str}", style="dim"),
                Text(f"tok: {tok_str}", style="dim"),
                Text(f"cpu: {cpu_str}", style=cpu_style or ""),
                key=f"sub-{pid}",
            )

            # Blank separator between sessions
            self.add_row(
                Text(""), Text(""), Text(""), Text(""), Text(""),
                Text(""), Text(""), Text(""), Text(""),
                key=f"gap-{pid}",
            )

        # ── Remote peer sessions (from Supabase) ─────────────────────────
        for peer in remote_peers:
            p_sid = peer.get("session_id", "?")
            p_repo = peer.get("repo", "\u2014")
            p_task = peer.get("task_name", "\u2014") or "\u2014"
            p_account = peer.get("account", "?")
            p_tool = peer.get("tool", "?")

            # Heartbeat staleness
            hb_str = ""
            hb_style = "dim"
            heartbeat_raw = peer.get("heartbeat_at", "")
            if heartbeat_raw:
                try:
                    hb_dt = datetime.fromisoformat(
                        heartbeat_raw.replace("Z", "+00:00")
                    )
                    hb_age_s = int((now_utc - hb_dt).total_seconds())
                    if hb_age_s < 60:
                        hb_str = "{}s ago".format(hb_age_s)
                        hb_style = "green"
                    elif hb_age_s < 600:
                        hb_str = "{}m ago".format(hb_age_s // 60)
                        hb_style = "dim"
                    else:
                        hb_str = "stale"
                        hb_style = "dim italic"
                except Exception:
                    hb_str = "?"

            # Claimed-at as start time
            claimed_str = ""
            claimed_raw = peer.get("claimed_at", "")
            if claimed_raw:
                try:
                    claimed_dt = datetime.fromisoformat(
                        claimed_raw.replace("Z", "+00:00")
                    ).astimezone()
                    claimed_str = claimed_dt.strftime("%H:%M:%S")
                except Exception:
                    claimed_str = "?"

            # Account color
            acct_color = {"A": "cyan", "B": "magenta", "C": "yellow"}.get(p_account, "dim")

            # Project/company from repo
            co_name, co_style = _project_to_company(p_repo)

            self.add_row(
                Text(claimed_str, style="dim"),
                Text.from_markup("[blue]\u2601 [/blue][dim]{}[/dim]".format(p_sid)),
                Text(p_tool, style="dim"),
                Text(p_account, style=acct_color),
                Text(p_repo, style="dim"),
                Text("\u2014", style="dim"),
                Text(hb_str, style=hb_style),
                Text("\u2014", style="dim"),
                Text(p_task),
                key="peer-{}".format(p_sid),
            )

            # Blank separator
            self.add_row(
                Text(""), Text(""), Text(""), Text(""), Text(""),
                Text(""), Text(""), Text(""), Text(""),
                key="peergap-{}".format(p_sid),
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row)
        except Exception:
            pass

    def _get_pid_from_cursor(self):
        # type: () -> Optional[str]
        """Extract PID from the currently selected row key."""
        try:
            key = self.get_row_at(self.cursor_row)
            # key is the row data, we need the row_key
            row_key = None
            for rk in self.rows:
                if rk == self.cursor_row:
                    row_key = rk
                    break
        except Exception:
            pass

        # Use the rows mapping: iterate to find current cursor row's key
        try:
            keys = list(self.rows.keys())
            if self.cursor_row < len(keys):
                row_key = keys[self.cursor_row]
                key_str = row_key.value if hasattr(row_key, "value") else str(row_key)
                if key_str.startswith("active-"):
                    return key_str.replace("active-", "")
                elif key_str.startswith("sub-"):
                    return key_str.replace("sub-", "")
                elif key_str.startswith("gap-"):
                    return key_str.replace("gap-", "")
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event):
        """Handle Enter key — focus the terminal for the selected session."""
        self._focus_terminal_for_row(event.row_key)

    def action_focus_selected(self):
        """Handle 'f' key — focus the terminal for the currently highlighted session."""
        pid = self._get_pid_from_cursor()
        if pid:
            ok = focus_session_terminal(pid)
            if ok:
                self.app.notify("Focused terminal", severity="information", timeout=2)
            else:
                self.app.notify("No matching window", severity="warning", timeout=2)

    def _focus_terminal_for_row(self, row_key):
        """Extract PID from row key and focus the corresponding terminal."""
        if not row_key:
            return
        key_str = row_key.value if hasattr(row_key, "value") else str(row_key)
        pid = None
        if key_str.startswith("active-"):
            pid = key_str.replace("active-", "")
        elif key_str.startswith("sub-"):
            pid = key_str.replace("sub-", "")
        elif key_str.startswith("gap-"):
            pid = key_str.replace("gap-", "")
        if pid and pid != "empty":
            ok = focus_session_terminal(pid)
            if ok:
                self.app.notify("Focused terminal", severity="information", timeout=2)
            else:
                self.app.notify("No matching window", severity="warning", timeout=2)


class ToolFrequency(Static):
    def update_content(self):
        self.update(make_tool_stats())


class SkillsPanel(Static):
    def update_content(self):
        self.update(make_skills_panel())


class AgentsPanel(Static):
    def update_content(self):
        from claude_watch_data import _get_agent_stats
        stats = _get_agent_stats(days=7)
        t = RichTable(
            show_header=True, header_style="bold yellow",
            box=None, padding=(0, 1), expand=True,
        )
        t.add_column("Agent Description", overflow="ellipsis", no_wrap=True, ratio=3)
        t.add_column("Spawns", min_width=7, justify="right", no_wrap=True)
        t.add_column("Last", min_width=6, no_wrap=True)
        if not stats:
            t.add_row(Text("no agent spawns yet", style="dim"), "", "")
        else:
            for desc, count, last in stats[:10]:
                t.add_row(
                    Text(desc, overflow="ellipsis"),
                    Text(str(count), justify="right"),
                    Text(last, style="dim"),
                )
        self.update(Panel(
            t,
            title="[bold]Agent Spawns[/bold]  [dim](7d)[/dim]",
            border_style="yellow",
        ))


class SessionNarrativePanel(Static):
    """Compact narrative of what was built in the current 5h window, grouped by project."""

    def update_content(self):
        # Get current 5h window bounds
        _, _, five_reset_ts, _ = _current_pct()
        window_start = None
        if five_reset_ts:
            try:
                reset_dt = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
                window_start = reset_dt - timedelta(hours=5)
            except Exception:
                pass

        # Filter session history to current window
        sessions = _get_session_history()
        if window_start:
            sessions = [s for s in sessions if s["last_ts"] >= window_start]

        if not sessions:
            self.update("")
            self.display = False
            return

        # Group by project
        from collections import defaultdict
        project_sessions = defaultdict(list)
        for s in sessions:
            project = s.get("project", "\u2014")
            project_sessions[project].append(s)

        # Build narrative lines
        lines = []
        # Color map for known projects
        color_map = {
            "atlas": "blue",
            "claude-watch": "cyan",
            "paperclip": "green",
            "openclaw": "magenta",
            "frank": "magenta",
            "kaa": "green",
        }

        for project, proj_sessions in sorted(project_sessions.items(), key=lambda x: len(x[1]), reverse=True):
            descriptions = []
            for s in proj_sessions:
                # Prefer directive as summary
                directive = s.get("directive", "")
                if directive and directive != "\u2014":
                    descriptions.append(directive)
                else:
                    # Fall back to gravity center from accomplishments
                    acc = _extract_accomplishments(s["session_id"])
                    gc = _gravity_center(acc, fallback="")
                    if gc:
                        descriptions.append(gc)

            if not descriptions:
                continue

            # Deduplicate while preserving order
            seen = set()
            unique = []
            for d in descriptions:
                d_lower = d.lower().strip()
                if d_lower not in seen:
                    seen.add(d_lower)
                    unique.append(d)

            # Build the description string — join with commas, truncate if needed
            desc_str = ", ".join(unique)
            if len(desc_str) > 100:
                desc_str = desc_str[:97] + "..."

            p_color = color_map.get(project.lower(), "white")
            lines.append(f"[bold {p_color}]{project}[/bold {p_color}]: {desc_str}")

        if not lines:
            self.update("")
            self.display = False
            return

        content = "\n".join(lines)
        self.update(Panel(
            content,
            title="[bold]Session Narrative[/bold]",
            border_style="green",
        ))
        self.display = True


class DrainPanel(Static):
    def update_content(self):
        self.update(make_drain_panel())



class TokenAttributionPanel(Static):
    """Compact per-session token attribution bar on main dashboard."""

    def update_content(self):
        data = _get_token_attribution()
        if not data or not data.get("sessions"):
            self.update("[dim]No attribution data[/dim]")
            self.display = False
            return
        self.display = True
        sessions = data["sessions"]
        total = data["total_used_pct"]
        unaccounted = data.get("unaccounted_pct", 0)
        try:
            bar_width = max(20, self.size.width - 6)
        except Exception:
            bar_width = 50
        bar_chars = []
        legend_parts = []
        for s in sessions:
            pct = s["pct_used"]
            if pct < 0.5:
                continue
            cols = max(1, int(pct / total * bar_width)) if total > 0 else 1
            color = s["color"]
            label = f"{pct:.0f}%"
            segment = label.center(cols) if cols >= len(label) + 2 else "█" * cols
            bar_chars.append(f"[bold white on {color}]{segment}[/]")
            directive = s["directive"][:20] if s["directive"] else s["session_id"][:12]
            legend_parts.append(f"[{color}]■[/] {directive} ({pct:.1f}%)")
        if unaccounted > 0.5:
            cols = max(1, int(unaccounted / total * bar_width)) if total > 0 else 1
            segment = f"{unaccounted:.0f}%".center(cols) if cols >= 6 else "░" * cols
            bar_chars.append(f"[dim]{segment}[/dim]")
            legend_parts.append(f"[dim]░ rolled out ({unaccounted:.1f}%)[/dim]")
        bar_line = "".join(bar_chars)
        legend_line = "  ".join(legend_parts)
        content_str = bar_line + chr(10) + legend_line
        self.update(Panel(
            content_str,
            title=f"[bold]Who Ate My {total:.0f}%?[/bold]",
            border_style="yellow",
        ))


class BurndownChart(Static):
    """Token burndown chart — full 5h window with past, now marker, and projected future."""

    _BLOCKS = " ▁▂▃▄▅▆▇█"

    def update_content(self):
        data = _get_burndown_data()
        if not data or not data.get("actual"):
            self.update("[dim]No burndown data yet[/dim]")
            return

        actual = data["actual"]
        remaining = data["remaining_pct"]
        rate = data["current_rate"]
        status = data["status"]
        mins_to_reset = data["mins_to_reset"]
        wall_mins = data.get("projected_wall_mins")
        proj_remaining = data.get("projected_remaining_at_reset", remaining)
        mins_elapsed = data["mins_elapsed"]
        mins_total = data["mins_total"]  # 300 min
        window_start = data["window_start"]
        window_reset = data["window_reset"]

        # Chart spans the FULL 5h window, edge to edge
        # Dynamic width: subtract frame (panel border + "100%│" prefix + "│" suffix)
        try:
            available = self.size.width - 10  # 2 border + 5 label + 2 bars + 1 pad
            chart_width = max(20, min(available, 70))
        except Exception:
            chart_width = 50
        now_col = int(mins_elapsed / mins_total * chart_width)
        now_col = max(1, min(now_col, chart_width - 1))

        # Build data for every column across the full window
        full_data = []  # type: list  # (remaining_pct, zone) per column
        for col in range(chart_width):
            col_min = col * mins_total / chart_width

            if col <= now_col:
                # PAST — use actual data (find closest point)
                closest = None
                for m, r in actual:
                    if closest is None or abs(m - col_min) < abs(closest[0] - col_min):
                        closest = (m, r)
                val = closest[1] if closest else 100.0
                full_data.append((val, "past"))
            else:
                # FUTURE — project from current remaining at current rate
                future_mins = col_min - mins_elapsed
                if rate > 0:
                    projected = max(0.0, remaining - rate * future_mins)
                else:
                    projected = remaining
                full_data.append((projected, "future"))

        # Ideal pace line: straight diagonal 100% → 0% across full window
        ideal_at = []  # type: list
        for col in range(chart_width):
            col_min = col * mins_total / chart_width
            ideal_at.append(max(0.0, 100.0 * (1.0 - col_min / mins_total)))

        # Budget per 10 minutes (to use it all evenly)
        budget_per_10 = (remaining / mins_to_reset * 10) if mins_to_reset > 0 else 0

        # Render 3 chart rows
        rows = []
        for row_idx in range(3):
            row_min = (2 - row_idx) * 33.3
            row_max = row_min + 33.3
            chars = []
            for col in range(chart_width):
                val, zone = full_data[col]
                ideal_val = ideal_at[col]

                # Now marker
                if col == now_col:
                    chars.append("[bold white]│[/bold white]")
                    continue

                # Map value to block char
                if val <= row_min:
                    block = " "
                elif val >= row_max:
                    block = "█"
                else:
                    frac = (val - row_min) / 33.3
                    idx = int(frac * 8)
                    block = self._BLOCKS[min(idx, 8)]

                if zone == "future":
                    # Future: show projection as dim line, ideal as dots
                    if block == " ":
                        if row_min < ideal_val < row_max:
                            chars.append("[dim green]·[/dim green]")
                        else:
                            chars.append(" ")
                    else:
                        chars.append(f"[dim]{block}[/dim]")
                else:
                    # Past: colored based on actual vs ideal
                    if block == " ":
                        if row_min < ideal_val < row_max:
                            chars.append("[dim]·[/dim]")
                        else:
                            chars.append(" ")
                    else:
                        if val > ideal_val + 10:
                            color = "green"
                        elif val > ideal_val - 10:
                            color = "yellow"
                        else:
                            color = "red"
                        chars.append(f"[{color}]{block}[/{color}]")

            rows.append("".join(chars))

        # Stats
        rate_color = "red" if rate > 3 else ("yellow" if rate > 1 else "green")
        remaining_color = "red" if remaining < 20 else ("yellow" if remaining < 40 else "green")

        if status == "critical":
            proj_str = f"[bold red]WALL in ~{wall_mins:.0f}m[/bold red]"
        elif status == "burning_fast" and wall_mins:
            proj_str = f"[yellow]Wall in ~{wall_mins:.0f}m[/yellow]"
        elif status == "wasting":
            proj_str = f"[yellow]~{proj_remaining:.0f}% wasted at reset[/yellow]"
        else:
            proj_str = f"[green]~{proj_remaining:.0f}% at reset[/green]"

        h_reset = int(mins_to_reset // 60)
        m_reset = int(mins_to_reset % 60)
        reset_str = f"{h_reset}h{m_reset:02d}m" if h_reset else f"{m_reset}m"

        budget_color = "green" if budget_per_10 < 5 else ("yellow" if budget_per_10 < 10 else "red")

        start_label = window_start.astimezone().strftime("%H:%M")
        now_label = datetime.now().strftime("%H:%M")
        reset_label = window_reset.astimezone().strftime("%H:%M")

        # Time axis — position labels under chart
        axis = [" "] * chart_width
        # Start label
        for i, c in enumerate(start_label):
            if i < chart_width:
                axis[i] = c
        # Now label (center on now_col)
        now_start = max(0, now_col - 2)
        for i, c in enumerate(now_label):
            pos = now_start + i
            if 0 <= pos < chart_width:
                axis[pos] = c
        # Reset label at end
        reset_start = max(0, chart_width - len(reset_label))
        for i, c in enumerate(reset_label):
            pos = reset_start + i
            if 0 <= pos < chart_width:
                axis[pos] = c
        axis_str = "".join(axis)

        # Bottom border with now marker
        border = []
        for col in range(chart_width):
            if col == now_col:
                border.append("[bold white]┴[/bold white]")
            else:
                border.append("─")
        border_str = "".join(border)

        # Pacing verdict — the key "am I wasting tokens?" indicator
        needed_rate = remaining / mins_to_reset if mins_to_reset > 0 else 0.0
        if remaining < 3:
            verdict = "[bold green]✓ USED UP[/bold green]"
        elif rate >= needed_rate * 0.9:
            verdict = "[bold green]✓ ON PACE[/bold green]"
        elif status == "critical":
            wall_str = f"~{wall_mins:.0f}m" if wall_mins else "soon"
            verdict = f"[bold red]⚡ WALL in {wall_str}[/bold red]"
        elif status == "burning_fast":
            verdict = "[bold yellow]⚡ FAST[/bold yellow]"
        elif status == "wasting" or rate < needed_rate * 0.5:
            wasted = proj_remaining if proj_remaining > 0 else 0
            verdict = f"[bold red]⚠ WASTING ~{wasted:.0f}%[/bold red]"
        else:
            verdict = "[yellow]~ SLOW[/yellow]"

        verdict_line = (
            f"{verdict}  [{rate_color}]{rate:.1f}%/min[/{rate_color}]"
            f"  →  [dim]{needed_rate:.1f}%/min needed[/dim]"
            f"  │  [dim]Resets in {reset_str}[/dim]"
        )

        if remaining < 3 or (rate >= needed_rate * 0.9 and status not in ("critical", "burning_fast")):
            proj_label = f"[green]~{proj_remaining:.0f}% at reset[/green]"
        else:
            proj_label = f"[yellow]~{proj_remaining:.0f}% wasted at reset[/yellow]"

        details_line = (
            f"[{remaining_color}]{remaining:.0f}% left[/{remaining_color}]"
            f"  │  [{budget_color}]Budget: {budget_per_10:.1f}%/10m[/{budget_color}]"
            f"  │  {proj_label}"
        )

        # ── Right side: converged Token Monitor info ──
        from claude_watch_data import (
            _current_pct, _countdown, _reset_day,
            _get_active_account, _token_pacing, _burn_mode,
        )
        five, seven, fr, sr = _current_pct()

        def mini_bar(pct, width=12):
            try:
                pct_f = float(pct)
                filled = int(pct_f * width / 100)
                color = "green" if pct_f < 50 else ("yellow" if pct_f < 75 else "red")
                return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}]"
            except Exception:
                return f"[dim]{'░' * width}[/dim]"

        used_pct = 100.0 - remaining
        used_color = "red" if used_pct > 80 else ("yellow" if used_pct > 60 else "green")
        left_color = "red" if remaining < 20 else ("yellow" if remaining < 40 else "green")

        label, name, lane = _get_active_account()
        acct_color = "cyan" if label == "A" else ("magenta" if label == "B" else "yellow")

        h_reset = int(mins_to_reset // 60)
        m_reset = int(mins_to_reset % 60)
        reset_str = f"{h_reset}h{m_reset:02d}m" if h_reset else f"{m_reset}m"

        # Pacing line
        pacing = _token_pacing()
        pace_str = ""
        if pacing:
            if pacing["status"] == "at_limit":
                pace_str = "[red bold]AT LIMIT[/red bold]"
            else:
                m100 = pacing["mins_to_100"]
                mr = pacing["mins_to_reset"]
                burn = pacing["avg_burn"]
                if m100 < mr:
                    pace_str = f"[yellow]100% in ~{m100:.0f}m[/yellow] at {burn:.1f}%/min"
                else:
                    pace_str = f"[green]OK[/green] at {burn:.1f}%/min"

        # Burn mode for title
        burn_active, burn_secs = _burn_mode()
        burn_title = ""
        if burn_active:
            bm, bs = burn_secs // 60, burn_secs % 60
            burn_title = f"  [bold magenta]BURN {bm}m {bs:02d}s[/bold magenta]"

        # Live window score
        from claude_watch_data import (
            _score_window as _sw, _get_window_scores, _get_streak, _stars_display,
        )
        live_score = _sw(window_start, window_reset)
        if live_score:
            stars = live_score["stars"]
            ov = live_score["overall"]
            star_color = "green" if ov >= 4 else ("yellow" if ov >= 3 else "red")
            def _dim_c(val):
                return "green" if val >= 4 else ("yellow" if val >= 2.5 else "red")
            b, p, sh, br, ve = live_score['burn'], live_score['parallelism'], live_score['shipping'], live_score['breadth'], live_score['velocity']
            score_line = (
                f"  [{star_color}]{stars} {ov}[/{star_color}]"
                f"  [dim]Burn:[/dim][{_dim_c(b)}]{b:.0f}[/{_dim_c(b)}]"
                f" [dim]Parallel:[/dim][{_dim_c(p)}]{p:.0f}[/{_dim_c(p)}]"
                f" [dim]Ship:[/dim][{_dim_c(sh)}]{sh:.0f}[/{_dim_c(sh)}]"
                f" [dim]Breadth:[/dim][{_dim_c(br)}]{br:.0f}[/{_dim_c(br)}]"
                f" [dim]Velocity:[/dim][{_dim_c(ve)}]{ve:.0f}[/{_dim_c(ve)}]"
            )
            streak = _get_streak()
            if streak >= 3:
                score_line += f"  [bold yellow]🔥{streak}-streak[/bold yellow]"
        else:
            score_line = ""

        # Token zone classification
        def _token_zone(pct):
            try:
                p = float(pct)
            except Exception:
                return ("?", "dim")
            if p < 40:
                return ("COOL", "green")
            if p < 70:
                return ("WARM", "yellow")
            if p < 85:
                return ("HOT", "red")
            return ("REDLINE", "bold red")

        five_zone, five_zcolor = _token_zone(five)
        seven_zone, seven_zcolor = _token_zone(seven)

        # Build right-side lines (aligned with chart rows)
        r = [
            f"  [bold {used_color}]{used_pct:.0f}% Used[/bold {used_color}]  [bold {left_color}]{remaining:.0f}% Left[/bold {left_color}]",
            f"  [bold]5h[/bold] {mini_bar(five)} {float(five):.0f}%  [dim]resets {reset_str}[/dim]",
            f"  [bold]7d[/bold] {mini_bar(seven)} {float(seven):.0f}%  [dim]{_reset_day(sr)[:10]}[/dim]",
            f"  [{five_zcolor}]{five_zone}[/{five_zcolor}] 5h  [{seven_zcolor}]{seven_zone}[/{seven_zcolor}] 7d",
            f"  [{acct_color}]Acct {label}[/{acct_color}]: {name} [dim]({lane})[/dim]",
            f"  {pace_str}",
            f"  {verdict}",
            score_line,
        ]

        lines = [
            f"100%│{rows[0]}│{r[0]}",
            f"    │{rows[1]}│{r[1]}",
            f"  0%│{rows[2]}│{r[2]}",
            f"    └{border_str}┘{r[3]}",
            f"     [dim]{axis_str}[/dim]{r[4]}",
            r[5],
            r[6],
            r[7],
        ]

        content = "\n".join(lines)
        self.update(
            Panel(content, title=f"[bold]Token Burndown[/bold]  [dim](5h window)[/dim]{burn_title}",
                  border_style="bright_blue")
        )


class SystemHealthPanel(Static):
    """System health — CPU and memory for Claude ecosystem processes."""

    def update_content(self):
        health = _get_system_health()
        if not health:
            self.update("")
            self.display = False
            return

        t = RichTable(show_header=True, header_style="bold", box=None, padding=(0, 1), expand=True)
        t.add_column("When", width=9, no_wrap=True)
        t.add_column("Process", width=10, no_wrap=True)
        t.add_column("Src", width=10, no_wrap=True)
        t.add_column("Co", width=8, no_wrap=True)
        t.add_column("Project", width=12, no_wrap=True)
        t.add_column("Mdl", width=10, no_wrap=True)
        t.add_column("Mem", width=8, justify="right", no_wrap=True)
        t.add_column("Status", overflow="ellipsis", no_wrap=True)

        # Build model map from ledger
        entries = _load_ledger(last_n=500)
        model_map = {}  # type: dict
        for e in entries:
            sid = e.get("session", "")
            mdl = e.get("model")
            if sid and mdl and mdl != "?":
                model_map[sid] = mdl

        # Claude sessions
        for s in health.get("claude_sessions", []):
            pid = s["pid"]
            cpu = s["cpu"]
            mem = s["mem_mb"]
            directive = s["directive"]
            st = s["status"]
            source = s.get("source", "?")

            # Derive project and company
            project = "—"
            if source in ("atlas-be", "atlas-fe"):
                project = "atlas"
            elif source in ("openclaw", "frank", "paperclip"):
                project = source
            elif "/" in source:
                project = source.split("/")[0].lower()
            else:
                d_lower = directive.lower() if directive else ""
                for p in ("claude-watch", "atlas", "paperclip", "openclaw", "frank"):
                    if p in d_lower:
                        project = p
                        break
            co_name, co_style = _project_to_company(project)

            src_color = (
                "yellow" if ("/" in source or source == "paperclip")
                else ("green" if source == "cli"
                       else ("cyan" if "atlas" in source else "dim"))
            )

            def mem_mini_gauge(mb):
                pct = min(mb / 10, 100)  # scale: 1000MB = 100%
                filled = min(int(pct * 3 / 100), 3)
                color = "green" if mb < 300 else ("yellow" if mb < 500 else "red")
                bar = f"[{color}]{'█' * filled}{'░' * (3 - filled)}[/{color}]"
                if mb >= 1024:
                    return f"{bar} {mb / 1024:.1f}GB"
                return f"{bar} {mb}MB"

            mem_str = mem_mini_gauge(mem)
            if st == "runaway":
                dot = "[bold red]⚠ [/bold red]"
                status_str = f"[bold red]runaway[/bold red] ({directive[:20]})"
            elif st == "active":
                dot = "[bold green]● [/bold green]"
                status_str = f"[green]active[/green] ({directive[:20]})"
            else:
                dot = "  "
                status_str = f"[dim]{st}[/dim]"

            start_time = s.get("start_time", "?")
            mdl = _abbrev_model(model_map.get(f"cc-{pid}", "?"))
            mdl_style = "magenta" if "opus" in mdl else ("cyan" if "sonnet" in mdl else "dim")
            t.add_row(
                f"[dim]{start_time}[/dim]",
                f"{dot}[cyan]cc-{pid}[/cyan]",
                f"[{src_color}]{source}[/{src_color}]",
                f"[{co_style}]{co_name}[/{co_style}]",
                f"[dim]{project}[/dim]",
                f"[{mdl_style}]{mdl}[/{mdl_style}]",
                Text.from_markup(mem_str),
                status_str,
            )

        # Infrastructure
        for inf in health.get("infrastructure", []):
            name = inf["name"]
            cpu = inf["cpu"]
            mem = inf["mem_mb"]
            count = inf["count"]
            pid = inf["pid"]

            mem_str = f"{mem/1024:.1f}GB" if mem >= 1024 else f"{mem:.0f}MB"
            display_name = f"{name} (x{count})" if count > 1 else name

            # Hog alert
            alert = ""
            if mem > 3000:
                alert = " [red]← hog[/red]"

            t.add_row(
                "",
                f"[dim]{display_name}[/dim]",
                "",
                "",
                "",
                "",
                mem_str,
                f"[dim]infra[/dim]{alert}",
            )

        # Totals
        totals = health.get("totals", {})
        total_cpu = totals.get("cpu", 0)
        total_mem = totals.get("mem_mb", 0)
        mem_pct = totals.get("mem_pct", 0)
        sys_mem = totals.get("system_mem_mb", 16384)

        total_mem_str = f"{total_mem/1024:.1f}GB" if total_mem >= 1024 else f"{total_mem:.0f}MB"
        mem_pct_color = "red" if mem_pct > 80 else ("yellow" if mem_pct > 60 else "green")

        # Summary gauge row
        def gauge_bar(pct, width=10):
            filled = int(pct * width / 100)
            color = "green" if pct < 40 else ("yellow" if pct < 70 else "red")
            return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}]"

        def zone_label(pct):
            if pct < 40:
                return ("COOL", "green")
            if pct < 70:
                return ("WARM", "yellow")
            if pct < 85:
                return ("HOT", "red")
            return ("REDLINE", "bold red")

        mem_zone, mem_zc = zone_label(mem_pct)
        cpu_capped = min(total_cpu, 100)
        cpu_zone, cpu_zc = zone_label(cpu_capped)
        mem_gb = total_mem / 1024
        sys_gb = sys_mem / 1024

        t.add_row(
            "",
            Text.from_markup(f"MEM {gauge_bar(mem_pct)} {mem_gb:.1f}GB/{sys_gb:.0f}GB [{mem_zc}]{mem_zone}[/{mem_zc}]"),
            "",
            "",
            "",
            Text.from_markup(f"CPU {gauge_bar(cpu_capped)} {total_cpu:.0f}% [{cpu_zc}]{cpu_zone}[/{cpu_zc}]"),
            "",
            "",
        )

        t.add_row(
            "",
            "[bold]Total AI stack[/bold]",
            "",
            "",
            "",
            "",
            f"[bold]{total_mem_str}[/bold]",
            f"[{mem_pct_color}]{mem_pct:.0f}% of {sys_mem/1024:.0f}GB[/{mem_pct_color}]",
        )

        self.display = True
        self.update(Panel(t, title="[bold]System Health[/bold]", border_style="magenta"))



class ReloadBanner(Static):
    """Banner shown when source files have changed. Click or press Shift+R to reload."""

    DEFAULT_CSS = """
    ReloadBanner {
        display: none;
        height: 1;
        dock: top;
        background: $warning;
        color: $text;
        text-align: center;
        text-style: bold;
    }
    ReloadBanner.reverted {
        background: $error;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._mode = "hidden"

    def show_pending(self):
        self._mode = "pending"
        self.update("[reverse] FILES CHANGED [/reverse]  Press [bold]Shift+R[/bold] to reload build")
        self.remove_class("reverted")
        self.display = True

    def show_reverted(self, error_msg=""):
        self._mode = "reverted"
        short_err = error_msg.strip().split("\n")[-1][:80] if error_msg else "import error"
        self.update(f"[reverse] BUILD BROKEN \u2014 REVERTED [/reverse]  {short_err}")
        self.add_class("reverted")
        self.display = True

    def hide_banner(self):
        self._mode = "hidden"
        self.display = False

    def on_click(self):
        if self._mode == "pending":
            self.app.action_reload_build()


# ── Navigation bar ───────────────────────────────────────────────────────────


class NavBar(Horizontal):
    """Top navigation bar with clickable buttons."""

    def __init__(self, active: str = "nav-dashboard", **kwargs):
        super().__init__(**kwargs)
        self._active = active

    def compose(self) -> ComposeResult:
        buttons = [
            ("Dashboard", "nav-dashboard"),
            ("Health", "nav-health"),
            ("Cycle", "nav-sessions"),
            ("Projects", "nav-projects"),
            ("Leaderboard", "nav-leaderboard"),
            ("Cycles", "nav-cycles"),
            ("Usage", "nav-usage"),
            ("MCP", "nav-mcp"),
        ]
        for label, btn_id in buttons:
            variant = "primary" if btn_id == self._active else "default"
            yield Button(label, id=btn_id, variant=variant)



class HealthScreen(Screen):
    """Full-screen system health view."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-health")
        yield Static(id="health-header")
        yield SystemHealthPanel(id="health-panel")

    def on_mount(self):
        self.query_one("#health-header", Static).update(
            "[bold]System Health[/bold]"
        )
        self.query_one("#health-panel", SystemHealthPanel).update_content()

    def action_pop_screen(self):
        self.app.pop_screen()


# ── Drill-down screen ────────────────────────────────────────────────────────


class SessionDrillDown(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
        Binding("t", "toggle_view", "Toggle tokens/accomplishments"),
    ]

    def __init__(self, session_id, directive="", project="—"):
        super().__init__()
        self.session_id = session_id
        self.session_directive = directive
        self.session_project = project
        self.showing_tokens = False

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-sessions")
        yield Static(
            f"[bold]Session:[/bold] {self.session_id}  "
            f"[bold]Project:[/bold] {self.session_project}  "
            f"[bold]Directive:[/bold] {self.session_directive}  "
            "[dim](t=toggle view)[/dim]",
            id="drilldown-header",
        )
        yield Static(id="accomplishments-view")
        yield DataTable(id="drilldown-table")

    def on_mount(self):
        self._show_accomplishments()
        self.query_one("#drilldown-table", DataTable).display = False

    def _show_accomplishments(self):
        acc = _extract_accomplishments(self.session_id)
        view = self.query_one("#accomplishments-view", Static)

        if not acc:
            view.update("[dim]No accomplishment data available.[/dim]")
            return

        lines = []

        # Summary bar
        turns = acc.get("turn_count", 0)
        files = len(acc.get("files_edited", [])) + len(acc.get("files_created", []))
        commits = len(acc.get("git_commits", []))
        errors = acc.get("errors", 0)
        # Get output tokens + model from index cache for cost estimate
        with _index_lock:
            idx_entry = _index_cache.get(self.session_id, {})
        out_tok = idx_entry.get("output_tokens", 0)
        session_cost = _estimate_cost(out_tok, idx_entry.get("model", ""))
        summary_parts = [f"[bold]{turns}[/bold] turns"]
        if files:
            summary_parts.append(f"[bold]{files}[/bold] files")
        if commits:
            summary_parts.append(f"[bold]{commits}[/bold] commits")
        if out_tok:
            cost_style = "red" if session_cost >= 2.0 else ("yellow" if session_cost >= 0.50 else "green")
            summary_parts.append(f"[{cost_style}]{_format_cost(session_cost)}[/{cost_style}]")
        if errors:
            summary_parts.append(f"[bold red]{errors}[/bold red] errors")
        lines.append("  ".join(summary_parts))
        lines.append("")

        # Git commits
        if acc.get("git_commits"):
            lines.append("[bold green]GIT COMMITS[/bold green]")
            for c in acc["git_commits"]:
                lines.append(f"  [green]•[/green] {c}")
            lines.append("")

        # Git pushes
        if acc.get("git_pushes"):
            lines.append("[bold cyan]PUSHED[/bold cyan]")
            for b in acc["git_pushes"]:
                lines.append(f"  [cyan]→[/cyan] {b}")
            lines.append("")

        # Files edited
        if acc.get("files_edited"):
            lines.append("[bold yellow]FILES EDITED[/bold yellow]")
            for fp in acc["files_edited"][:15]:
                lines.append(f"  [yellow]✎[/yellow] {fp}")
            if len(acc["files_edited"]) > 15:
                lines.append(f"  [dim]...and {len(acc['files_edited']) - 15} more[/dim]")
            lines.append("")

        # Files created
        if acc.get("files_created"):
            lines.append("[bold blue]FILES CREATED[/bold blue]")
            for fp in acc["files_created"][:10]:
                lines.append(f"  [blue]+[/blue] {fp}")
            lines.append("")

        # Skills
        if acc.get("skills"):
            lines.append("[bold magenta]SKILLS USED[/bold magenta]")
            for s in acc["skills"]:
                lines.append(f"  [magenta]⚡[/magenta] /{s}")
            lines.append("")

        # MCP operations
        if acc.get("mcp_ops"):
            lines.append("[bold cyan]MCP OPERATIONS[/bold cyan]")
            for op in acc["mcp_ops"][:10]:
                lines.append(f"  [cyan]⟐[/cyan] {op}")
            if len(acc["mcp_ops"]) > 10:
                lines.append(f"  [dim]...and {len(acc['mcp_ops']) - 10} more[/dim]")
            lines.append("")

        # Notable commands
        if acc.get("bash_notable"):
            lines.append("[bold]NOTABLE COMMANDS[/bold]")
            for cmd in acc["bash_notable"][:8]:
                lines.append(f"  [dim]$[/dim] {cmd}")
            lines.append("")

        # User prompts
        if acc.get("user_prompts"):
            lines.append("[bold]USER PROMPTS[/bold]")
            for p in acc["user_prompts"]:
                lines.append(f"  [dim]>[/dim] {p}")
            lines.append("")

        if not any(acc.get(k) for k in ("git_commits", "files_edited", "files_created",
                                         "skills", "mcp_ops", "bash_notable", "user_prompts")):
            lines.append("[dim]No significant accomplishments recorded.[/dim]")

        view.update("\n".join(lines))

    def _show_tokens(self):
        table = self.query_one("#drilldown-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("#", width=4)
        table.add_column("In", width=8)
        table.add_column("Out", width=8)
        table.add_column("~5h%", width=6)
        table.add_column("Model", width=7)
        table.add_column("Tools", width=25)
        table.add_column("Prompt")

        turns = _get_session_turns(self.session_id)
        if not turns:
            table.add_row("—", "", "", "", "", "", Text("no turns found", style="dim"))
            return

        total_in = total_out = total_pct = 0
        for t in turns:
            tokens_in = t["tokens_in"]
            tokens_out = t["tokens_out"]
            total_in += tokens_in
            total_out += tokens_out
            total_pct += t["pct_est"]

            in_str = f"{tokens_in/1000:.1f}k" if tokens_in >= 1000 else str(tokens_in)
            out_str = f"{tokens_out/1000:.1f}k" if tokens_out >= 1000 else str(tokens_out)

            pct = t["pct_est"]
            pct_style = "red" if pct > 1 else ("yellow" if pct > 0.3 else "dim")
            mdl_style = "magenta" if t["model"] == "opus" else ("cyan" if t["model"] == "sonnet" else "dim")

            table.add_row(
                str(t["turn"]),
                Text(in_str, style="dim"),
                Text(out_str),
                Text(f"{pct:.1f}%", style=pct_style),
                Text(t["model"], style=mdl_style),
                Text(t["tools"][:25], style="dim"),
                Text(t["prompt"][:50]),
            )

        table.add_row(
            Text("Σ", style="bold"),
            Text(f"{total_in/1000:.0f}k", style="bold"),
            Text(f"{total_out/1000:.0f}k", style="bold"),
            Text(f"{total_pct:.1f}%", style="bold yellow"),
            "",
            "",
            Text(f"{len(turns)} turns", style="bold"),
        )

    def action_toggle_view(self):
        self.showing_tokens = not self.showing_tokens
        acc_view = self.query_one("#accomplishments-view", Static)
        table = self.query_one("#drilldown-table", DataTable)

        if self.showing_tokens:
            acc_view.display = False
            table.display = True
            self._show_tokens()
        else:
            table.display = False
            acc_view.display = True

    def action_pop_screen(self):
        self.app.pop_screen()


class DailySparklinePanel(Static):
    _SPARKS = " ▁▂▃▄▅▆▇█"

    def update_content(self):
        from claude_watch_data import _get_daily_usage
        data = _get_daily_usage(days=7)
        if not data:
            self.update(Panel("[dim]No data yet[/dim]", title="7-Day Output Tokens", border_style="cyan"))
            return

        values = [v for _, v in data]
        max_val = max(values) if any(v > 0 for v in values) else 1

        spark_chars = []
        for v in values:
            idx = int(v / max_val * 8) if max_val else 0
            spark_chars.append(self._SPARKS[min(idx, 8)])

        # Align: each column is 5 chars wide (3 label + 2 separator)
        spark_line = "  ".join(f"  {c}  " for c in spark_chars)
        label_line = "  ".join(f"{label[:5]:5}" for label, _ in data)
        count_line = "  ".join(
            f"{v // 1000:3}k " if v >= 1000 else f" ~0  "
            for _, v in data
        )

        content = "\n".join([
            f"[bold cyan]{spark_line}[/bold cyan]",
            f"[dim]{label_line}[/dim]",
            f"[dim]{count_line}[/dim]",
        ])
        self.update(Panel(
            content,
            title="[bold]7-Day Output Tokens[/bold]",
            border_style="cyan",
        ))


class UsageMetricsView(LazyView):

    def compose(self) -> ComposeResult:
        yield Static(id="metrics-header")
        yield DailySparklinePanel(id="metrics-sparkline")
        yield DataTable(id="metrics-table")
        yield Static(id="metrics-summary")
        yield Static(id="scores-header")
        yield DataTable(id="scores-table")

    def load_content(self):
        metrics, total = _get_usage_metrics(days=7)
        self.query_one("#metrics-sparkline", DailySparklinePanel).update_content()
        _, seven, _, _ = _current_pct()

        # Estimate total cost across all sources
        total_cost = sum(
            _estimate_cost(m["output_tokens"], m.get("model", "sonnet"))
            for m in metrics
        )

        self.query_one("#metrics-header", Static).update(
            f"[bold]Usage Metrics — last 7 days[/bold]  "
            f"[dim]Total output: {total/1000:.0f}k tokens  "
            f"Est. cost: [/dim][yellow]{_format_cost(total_cost)}[/yellow]  "
            f"[dim]Account 7d: {seven}%[/dim]"
        )

        table = self.query_one("#metrics-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_column("Source", width=20)
        table.add_column("Sessions", width=9)
        table.add_column("Output Tok", width=11)
        table.add_column("Avg/Session", width=12)
        table.add_column("% of Total", width=11)
        table.add_column("Share")

        for m in metrics:
            src = m["source"]
            src_style = "yellow" if ("/" in src or src == "paperclip") else (
                "green" if src == "cli" else ("cyan" if "atlas" in src else "dim")
            )
            out_k = m["output_tokens"]
            out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)
            avg_k = m["avg_tokens"]
            avg_str = f"{avg_k/1000:.1f}k" if avg_k >= 1000 else str(avg_k)
            pct = m["pct_of_total"]
            bar_len = max(1, int(pct / 2.5))  # 40 chars = 100%
            bar = "█" * bar_len + "░" * (40 - bar_len)
            bar_color = "yellow" if ("/" in src) else ("green" if src == "cli" else "cyan")
            table.add_row(
                Text(src, style=src_style),
                Text(str(m["sessions"]), justify="right"),
                Text(out_str, justify="right"),
                Text(avg_str, justify="right"),
                Text(f"{pct:.1f}%", justify="right"),
                Text(bar[:40], style=bar_color),
            )

        self.query_one("#metrics-summary", Static).update(
            f"[dim]Sessions above represent all indexed transcripts from the last 7 days. "
            f"7d account budget usage ({seven}%) is account-level and not split per source.[/dim]"
        )

        # Window Scores
        from claude_watch_data import _get_window_scores, _get_streak, _stars_display
        scores = _get_window_scores(limit=10)
        streak = _get_streak(scores)

        streak_str = f"  [bold yellow]🔥 {streak}-window streak[/bold yellow]" if streak >= 3 else ""
        self.query_one("#scores-header", Static).update(
            f"[bold]Window Scores[/bold]  [dim]{len(scores)} scored windows[/dim]{streak_str}"
        )

        st = self.query_one("#scores-table", DataTable)
        st.cursor_type = "row"
        st.zebra_stripes = True
        st.add_column("Window", width=18)
        st.add_column("Stars", width=8)
        st.add_column("Overall", width=8)
        st.add_column("Burn", width=6)
        st.add_column("Para", width=6)
        st.add_column("Ship", width=6)
        st.add_column("Breadth", width=8)
        st.add_column("Vel", width=6)
        st.add_column("Details")

        for s in scores:
            try:
                ws = datetime.fromisoformat(s["window_start"].replace("Z", "+00:00"))
                window_label = ws.astimezone().strftime("%b %d %H:%M")
            except Exception:
                window_label = "?"
            ov = s.get("overall", 0)
            ov_color = "green" if ov >= 4 else ("yellow" if ov >= 3 else "red")
            details = (
                f"{s.get('burn_pct', 0):.0f}% burn, "
                f"{s.get('max_parallel', 0)} parallel, "
                f"{s.get('commits', 0)} commits, "
                f"{s.get('projects', 0)} projects"
            )
            st.add_row(
                Text(window_label, style="dim"),
                Text(s.get("stars", "?"), style=ov_color),
                Text(f"{ov}", style=ov_color, justify="right"),
                Text(f"{s.get('burn', 0):.0f}", justify="right"),
                Text(f"{s.get('parallelism', 0):.0f}", justify="right"),
                Text(f"{s.get('shipping', 0):.0f}", justify="right"),
                Text(f"{s.get('breadth', 0):.0f}", justify="right"),
                Text(f"{s.get('velocity', 0):.0f}", justify="right"),
                Text(details, style="dim"),
            )

        if not scores:
            st.add_row(
                Text("No scored windows yet", style="dim"),
                "", "", "", "", "", "", "", "",
            )


class MCPStatsView(LazyView):

    def compose(self) -> ComposeResult:
        yield Static(id="mcp-header")
        with Horizontal(id="mcp-body"):
            yield DataTable(id="mcp-servers-table")
            yield DataTable(id="mcp-actions-table")

    def load_content(self):
        from claude_watch_data import _get_mcp_stats
        stats = _get_mcp_stats(days=7)

        self.query_one("#mcp-header", Static).update(
            f"[bold]MCP Tool Usage — last 7 days[/bold]  "
            f"[dim]Total calls: {stats['total_calls']}  "
            f"Sessions using MCP: {stats['sessions_with_mcp']}[/dim]"
        )

        st = self.query_one("#mcp-servers-table", DataTable)
        st.cursor_type = "row"
        st.zebra_stripes = True
        st.add_column("Server", width=18)
        st.add_column("Calls", width=7)
        st.add_column("Top Actions")
        for s in stats["by_server"]:
            top3 = ", ".join(a for a, _ in s["actions"][:3])
            st.add_row(
                Text(s["server"], style="cyan"),
                Text(str(s["calls"]), justify="right"),
                Text(top3, style="dim"),
            )
        if not stats["by_server"]:
            st.add_row(Text("no MCP calls in last 7 days", style="dim"), "", "")

        at = self.query_one("#mcp-actions-table", DataTable)
        at.cursor_type = "row"
        at.zebra_stripes = True
        at.add_column("Action", width=40)
        at.add_column("Count", width=7)
        for action, count in stats["top_actions"]:
            server, _, act = action.partition(":")
            at.add_row(
                Text.from_markup(f"[cyan]{server}[/cyan][dim]:{act}[/dim]"),
                Text(str(count), justify="right"),
            )
        if not stats["top_actions"]:
            at.add_row(Text("no data", style="dim"), "")


class SessionTasksView(LazyView):
    """Cycle Monitor — freeform items for the current 5h window."""

    CAT_ICONS = {"bug": "\U0001f41b", "task": "\u2610", "idea": "\U0001f4a1", "direction": "\U0001f9ed"}
    STATUS_ICONS = {"open": "\u25cf", "done": "\u2713", "rolled": "\u2192"}
    CAT_ORDER = ["bug", "task", "idea", "direction"]
    PROJECTS = ["", "atlas", "claude-watch", "paperclip", "openclaw", "frank", "kaa"]
    PROJECT_LABELS = {"": "None", "atlas": "Atlas", "claude-watch": "CW", "paperclip": "Paper",
                      "openclaw": "OClaw", "frank": "Frank", "kaa": "KAA"}

    BINDINGS = [
        Binding("n", "focus_add", "New"),
        Binding("enter", "edit_item", "Edit"),
        Binding("x", "toggle_done", "Done"),
        Binding("r", "roll_item", "Roll"),
        Binding("d", "delete_item", "Delete"),
        Binding("slash", "start_filter", "Filter"),
        Binding("a", "show_all", "All"),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import Input
        yield Static(id="cm-header")
        with Horizontal(id="cm-add-row"):
            yield Input(id="cm-add-input", placeholder="Add item... (Tab=cat, Shift+Tab=project, Enter=save)")
            yield Button("Task \u2610", id="cm-cat-task", classes="cm-cat", variant="primary")
            yield Button("Bug \U0001f41b", id="cm-cat-bug", classes="cm-cat", variant="default")
            yield Button("Idea \U0001f4a1", id="cm-cat-idea", classes="cm-cat", variant="default")
            yield Button("Dir \U0001f9ed", id="cm-cat-dir", classes="cm-cat", variant="default")
        with Horizontal(id="cm-project-row"):
            yield Button("All", id="cm-proj-all", classes="cm-proj")
            yield Button("None", id="cm-proj-none", classes="cm-proj", variant="primary")
            yield Button("Atlas", id="cm-proj-atlas", classes="cm-proj")
            yield Button("CW", id="cm-proj-claude-watch", classes="cm-proj")
            yield Button("Paper", id="cm-proj-paperclip", classes="cm-proj")
            yield Button("OClaw", id="cm-proj-openclaw", classes="cm-proj")
            yield Button("Frank", id="cm-proj-frank", classes="cm-proj")
            yield Button("KAA", id="cm-proj-kaa", classes="cm-proj")
        yield DataTable(id="cm-table")
        yield Static(id="cm-prev")

    def load_content(self):
        self._category = "task"
        self._project = ""
        self._items = []
        self._window_start = ""
        self._editing_id = None
        self._filtering = False
        self._filter_text = ""
        self._show_all_windows = False

        # Compute window_start from burndown data
        bd = _get_burndown_data()
        if bd and bd.get("window_start"):
            ws = bd["window_start"]
            if isinstance(ws, datetime):
                self._window_start = ws.isoformat()
            else:
                self._window_start = str(ws)
        else:
            # Fallback: compute from current time
            now_utc = datetime.now(timezone.utc)
            self._window_start = now_utc.isoformat()

        dt = self.query_one("#cm-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Cat", width=5)
        dt.add_column("St", width=3)
        dt.add_column("Title", width=45)
        dt.add_column("Project", width=12)
        dt.add_column("Age", width=8)

        self._reload()

    @staticmethod
    def _fmt_age(created_at_str):
        """Format age of an item as short string."""
        try:
            created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            mins = int((datetime.now(timezone.utc) - created).total_seconds() / 60)
            if mins < 60:
                return f"{mins}m"
            if mins < 1440:
                return f"{mins // 60}h"
            return f"{mins // 1440}d"
        except Exception:
            return ""

    def _reload(self):
        from claude_watch_data import (
            _get_cycle_items, _get_recent_cycle_summaries, _get_current_cycle,
        )

        # Fetch items
        self._items = _get_cycle_items(self._window_start, all_windows=self._show_all_windows)

        # Apply filter if active
        display_items = self._items
        if self._filter_text:
            ft = self._filter_text.lower()
            display_items = [
                i for i in self._items
                if ft in (i.get("title") or "").lower()
                or ft in (i.get("project") or "").lower()
            ]

        # Rebuild table
        dt = self.query_one("#cm-table", DataTable)
        dt.clear()

        open_count = sum(1 for i in display_items if i.get("status") == "open")
        done_count = sum(1 for i in display_items if i.get("status") == "done")

        # Group by category
        groups = {}
        for item in display_items:
            cat = item.get("category", "task")
            groups.setdefault(cat, []).append(item)

        first_group = True
        for cat in self.CAT_ORDER:
            if cat not in groups:
                continue
            if not first_group:
                # Separator row
                dt.add_row(
                    Text("---", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    Text("", style="dim"),
                    key=f"sep-{cat}",
                )
            first_group = False
            for item in groups[cat]:
                cat_icon = self.CAT_ICONS.get(cat, "\u2610")
                status = item.get("status", "open")
                st_icon = self.STATUS_ICONS.get(status, "\u25cf")
                st_style = "green" if status == "done" else ("dim" if status == "rolled" else "")
                title = item.get("title", "")[:45]
                project = item.get("project", "")[:12]
                age = self._fmt_age(item.get("created_at", ""))

                dt.add_row(
                    Text(cat_icon),
                    Text(st_icon, style=st_style),
                    Text(title, style="strike" if status == "done" else ""),
                    Text(project, style="cyan"),
                    Text(age, style="dim"),
                    key=f"ci-{item['id']}",
                )

        if not display_items:
            dt.add_row(
                Text(""),
                Text(""),
                Text("No items yet — press n to add", style="dim"),
                Text(""),
                Text(""),
            )

        # Header
        bd = _get_burndown_data()
        mins_left = int(bd.get("mins_to_reset", 0)) if bd else 0
        hrs = mins_left // 60
        mins = mins_left % 60
        time_str = f"{hrs}h{mins:02d}m" if hrs else f"{mins}m"

        cycle = _get_current_cycle()
        stars = cycle.get("stars", "") if cycle else ""

        filter_str = f"  [yellow][filter: \"{self._filter_text}\"][/yellow]" if self._filter_text else ""
        mode_label = "[bold yellow]ALL CYCLES[/bold yellow]" if self._show_all_windows else f"resets in {time_str}  {stars}"
        self.query_one("#cm-header", Static).update(
            f"[bold]CYCLE MONITOR v2[/bold]  {mode_label}  "
            f"[green]{open_count} open[/green]  [dim]{done_count} done[/dim]{filter_str}  "
            f"[dim](n=add  /=filter  a=all  Enter=edit  x=done  r=roll  d=delete  q=back)[/dim]"
        )

        # Previous cycles
        summaries = _get_recent_cycle_summaries(limit=3)
        lines = []
        for s in summaries:
            parts = [s.get("when_str", "?")]
            if s.get("stars"):
                parts.append(s["stars"])
            total = s.get("items_total", 0)
            done = s.get("items_done", 0)
            rolled = s.get("items_rolled", 0)
            detail = f"{total} items ({done} done"
            if rolled:
                detail += f", {rolled} rolled"
            detail += ")"
            parts.append(detail)
            projs = s.get("projects", [])
            if projs:
                parts.append(", ".join(projs))
            lines.append("  ".join(parts))
        self.query_one("#cm-prev", Static).update(
            "\n".join(lines) if lines else "[dim]No previous cycles[/dim]"
        )

    def _get_item_by_row_key(self, row_key_str):
        """Find item dict by row key string like 'ci-<uuid>'."""
        if not row_key_str or row_key_str.startswith("sep-"):
            return None
        item_id = row_key_str.removeprefix("ci-")
        for item in self._items:
            if str(item.get("id")) == item_id:
                return item
        return None

    def action_start_filter(self):
        from textual.widgets import Input
        self._filtering = True
        inp = self.query_one("#cm-add-input", Input)
        inp.value = self._filter_text
        inp.placeholder = "Filter items... (Enter=apply, Esc=clear)"
        inp.focus()

    def action_show_all(self):
        self._filtering = False
        self._filter_text = ""
        from textual.widgets import Input
        inp = self.query_one("#cm-add-input", Input)
        inp.placeholder = "Add item... (Tab=cat, Shift+Tab=project, Enter=save)"
        self._reload()
        self.query_one("#cm-table", DataTable).focus()

    def on_input_changed(self, event):
        from textual.widgets import Input
        if event.input != self.query_one("#cm-add-input", Input):
            return
        if self._filtering:
            self._filter_text = event.value.strip()
            self._reload()

    def action_focus_add(self):
        from textual.widgets import Input
        self.query_one("#cm-add-input", Input).focus()

    def _update_project_buttons(self):
        all_btn = self.query_one("#cm-proj-all", Button)
        all_btn.variant = "primary" if self._show_all_windows else "default"
        for proj in self.PROJECTS:
            pid = proj or "none"
            btn = self.query_one(f"#cm-proj-{pid}", Button)
            if self._show_all_windows:
                btn.variant = "default"
            else:
                btn.variant = "primary" if proj == self._project else "default"

    def on_key(self, event):
        from textual.widgets import Input
        inp = self.query_one("#cm-add-input", Input)
        if not inp.has_focus:
            return
        if event.key == "escape" and self._filtering:
            event.prevent_default()
            event.stop()
            self._filtering = False
            self._filter_text = ""
            inp.value = ""
            inp.placeholder = "Add item... (Tab=cat, Shift+Tab=project, Enter=save)"
            self._reload()
            self.query_one("#cm-table", DataTable).focus()
            return
        if event.key == "tab":
            event.prevent_default()
            event.stop()
            cats = ["task", "bug", "idea", "direction"]
            idx = cats.index(self._category) if self._category in cats else 0
            self._category = cats[(idx + 1) % len(cats)]
            # Update button variants
            cat_map = {"task": "#cm-cat-task", "bug": "#cm-cat-bug",
                       "idea": "#cm-cat-idea", "direction": "#cm-cat-dir"}
            for cat, btn_id in cat_map.items():
                btn = self.query_one(btn_id, Button)
                btn.variant = "primary" if cat == self._category else "default"
        elif event.key == "shift+tab":
            event.prevent_default()
            event.stop()
            idx = self.PROJECTS.index(self._project) if self._project in self.PROJECTS else 0
            self._project = self.PROJECTS[(idx + 1) % len(self.PROJECTS)]
            self._update_project_buttons()

    def on_input_submitted(self, event):
        from textual.widgets import Input
        from claude_watch_data import _post_cycle_item, _update_cycle_item
        inp = self.query_one("#cm-add-input", Input)
        if event.input != inp:
            return
        if self._filtering:
            self._filtering = False
            self._filter_text = event.value.strip()
            inp.placeholder = "Add item... (Tab=cat, Shift+Tab=project, Enter=save)"
            self._reload()
            self.query_one("#cm-table", DataTable).focus()
            return
        title = event.value.strip()
        if not title:
            return
        if self._editing_id:
            _update_cycle_item(self._editing_id, {"title": title, "category": self._category, "project": self._project})
            self._editing_id = None
        else:
            _post_cycle_item(self._window_start, self._category, title, project=self._project)
        inp.value = ""
        self._reload()
        self.query_one("#cm-table", DataTable).focus()

    def on_button_pressed(self, event):
        cat_map = {
            "cm-cat-task": "task", "cm-cat-bug": "bug",
            "cm-cat-idea": "idea", "cm-cat-dir": "direction",
        }
        btn_id = event.button.id or ""
        if btn_id in cat_map:
            self._category = cat_map[btn_id]
            for bid, cat in cat_map.items():
                btn = self.query_one(f"#{bid}", Button)
                btn.variant = "primary" if cat == self._category else "default"
        elif btn_id == "cm-proj-all":
            self._show_all_windows = not self._show_all_windows
            self._update_project_buttons()
            self._reload()
        elif btn_id.startswith("cm-proj-"):
            proj_key = btn_id.removeprefix("cm-proj-")
            self._project = "" if proj_key == "none" else proj_key
            self._show_all_windows = False
            self._update_project_buttons()
            self._reload()

    def action_edit_item(self):
        from textual.widgets import Input
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        if row_key_str.startswith("sep-"):
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        # Populate input with item's title
        inp = self.query_one("#cm-add-input", Input)
        inp.value = item.get("title", "")
        # Set category and update buttons
        self._category = item.get("category", "task")
        cat_map = {"task": "#cm-cat-task", "bug": "#cm-cat-bug",
                   "idea": "#cm-cat-idea", "direction": "#cm-cat-dir"}
        for cat, btn_id in cat_map.items():
            btn = self.query_one(btn_id, Button)
            btn.variant = "primary" if cat == self._category else "default"
        # Set project and update buttons
        self._project = item.get("project", "")
        self._update_project_buttons()
        # Store editing state
        self._editing_id = item["id"]
        # Focus input
        inp.focus()

    def on_data_table_row_selected(self, event):
        """Handle click/Enter on a row — open edit mode."""
        if event.data_table.id != "cm-table":
            return
        self.action_edit_item()

    def action_toggle_done(self):
        from claude_watch_data import _update_cycle_item
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key = dt.get_row_at(dt.cursor_row)
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        new_status = "open" if item.get("status") == "done" else "done"
        updates = {"status": new_status}
        if new_status == "done":
            updates["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            updates["resolved_at"] = None
        _update_cycle_item(item["id"], updates)
        self._reload()

    def action_roll_item(self):
        from claude_watch_data import _update_cycle_item
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        _update_cycle_item(item["id"], {
            "status": "rolled",
            "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        self._reload()

    def action_delete_item(self):
        from claude_watch_data import _delete_cycle_item
        dt = self.query_one("#cm-table", DataTable)
        if dt.cursor_row is None:
            return
        try:
            row_key_str = str(dt._row_order[dt.cursor_row])
        except Exception:
            return
        item = self._get_item_by_row_key(row_key_str)
        if not item:
            return
        _delete_cycle_item(item["id"])
        self._reload()


class ProjectBoardView(LazyView):
    """Project Monitor — strategic task board."""

    def compose(self) -> ComposeResult:
        yield Static(id="pboard-header")
        yield Static(id="pboard-summary")
        yield DataTable(id="pboard-table")

    def load_content(self):
        from claude_watch_data import _get_project_tasks
        tasks = _get_project_tasks()

        total = len(tasks)
        by_status = {}
        by_project = {}
        total_points = 0
        done_points = 0
        dispatch_ready = 0
        total_tokens_k = 0
        for t in tasks:
            s = t.get("status", "?")
            p = t.get("project", "?")
            pts = t.get("points") or 0
            by_status[s] = by_status.get(s, 0) + 1
            if p not in by_project:
                by_project[p] = {"ready": 0, "in_progress": 0, "built": 0, "blocked": 0}
            if s in by_project[p]:
                by_project[p][s] += 1
            total_points += pts
            if s == "built":
                done_points += pts
            if s == "ready" and t.get("dispatch_prompt") and (t.get("tier") or "auto") == "auto" and not t.get("blocked_by"):
                dispatch_ready += 1
            if s in ("ready", "in_progress"):
                total_tokens_k += t.get("est_tokens_k") or 0

        ready = by_status.get("ready", 0)
        in_prog = by_status.get("in_progress", 0)
        built = by_status.get("built", 0)
        blocked = by_status.get("blocked", 0)
        remaining_points = total_points - done_points

        self.query_one("#pboard-header", Static).update(
            f"[bold]Project Board[/bold]  "
            f"[yellow]{ready} ready[/yellow]  "
            f"[green]{in_prog} active[/green]  "
            f"[dim]{built} built  {blocked} blocked  {total} total[/dim]  "
            f"│  [bold magenta]{dispatch_ready} dispatchable[/bold magenta]  "
            f"[cyan]{remaining_points}pts left[/cyan]  "
            f"[magenta]~{total_tokens_k}kT queued[/magenta]"
        )

        # Top panel: project summary
        summary_table = RichTable(show_header=True, show_edge=False, pad_edge=False, expand=True)
        summary_table.add_column("Co", style="dim")
        summary_table.add_column("Project", style="bold")
        summary_table.add_column("Rdy", style="yellow", justify="right")
        summary_table.add_column("Act", style="green", justify="right")
        summary_table.add_column("Blt", style="dim", justify="right")
        summary_table.add_column("Pts", style="cyan", justify="right")

        for proj in sorted(by_project.keys()):
            counts = by_project[proj]
            co_name, co_style = _project_to_company(proj)
            proj_pts = sum(t.get("points") or 0 for t in tasks
                          if t.get("project") == proj and t.get("status") in ("ready", "in_progress"))
            summary_table.add_row(
                Text(co_name, style=co_style),
                proj,
                str(counts["ready"]),
                str(counts["in_progress"]),
                str(counts["built"]),
                str(proj_pts) if proj_pts else "—",
            )

        self.query_one("#pboard-summary", Static).update(
            Panel(summary_table, title="[bold]Summary[/bold]", border_style="cyan")
        )

        # Bottom panel: task list (in_progress first, then ready)
        dt = self.query_one("#pboard-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=5)
        dt.add_column("Pri", width=4)
        dt.add_column("Diff", width=5)
        dt.add_column("Pts", width=3)
        dt.add_column("Tier", width=5)
        dt.add_column("~kT", width=4)
        dt.add_column("St", width=12)
        dt.add_column("Project", width=10)
        dt.add_column("Task", width=35)
        dt.add_column("🚀", width=2)  # dispatch-ready indicator

        # Sort: in_progress first, then ready, then blocked, then built; within each, by priority
        status_order = {"in_progress": 0, "ready": 1, "blocked": 2, "built": 3, "archived": 4}
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_tasks = sorted(tasks, key=lambda t: (
            status_order.get(t.get("status", ""), 9),
            priority_order.get((t.get("priority") or "medium").lower(), 9),
            t.get("build_order") or 9999,
        ))

        # Show in_progress + ready + blocked (skip built for readability, they can scroll)
        shown = [t for t in sorted_tasks if t.get("status") in ("in_progress", "ready", "blocked")]
        # Add up to 10 built at the end
        built_tasks = [t for t in sorted_tasks if t.get("status") == "built"][:10]
        shown.extend(built_tasks)

        _pri_label = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3"}
        _pri_style = {"critical": "bold red", "high": "bold yellow", "medium": "cyan", "low": "dim"}
        _diff_label = {"quick": "⚡", "easy": "📝", "medium": "🔨", "complex": "⚙️", "major": "🏗️"}
        _diff_style = {"quick": "green", "easy": "blue", "medium": "yellow", "complex": "bold yellow", "major": "bold red"}
        _tier_style = {"auto": "green bold", "assisted": "yellow", "manual": "red"}

        for t in shown:
            tid = str(t.get("id", ""))
            status = t.get("status", "?")
            status_icon = {"in_progress": "●", "ready": "○", "blocked": "◼", "built": "✓", "archived": "—"}.get(status, "?")
            status_style = {"in_progress": "green bold", "ready": "yellow", "blocked": "red", "built": "dim"}.get(status, "")
            pri = (t.get("priority") or "medium").lower()
            diff = (t.get("difficulty") or "").lower()
            tier = (t.get("tier") or "auto").lower()
            pts = t.get("points")
            tok = t.get("est_tokens_k")
            has_prompt = bool(t.get("dispatch_prompt"))
            blocked = t.get("blocked_by")

            # Dispatch-ready: has prompt, is auto tier, not blocked
            dispatch_ready = has_prompt and tier == "auto" and not blocked
            dispatch_icon = "✓" if dispatch_ready else ("⊘" if blocked else "")
            dispatch_style = "green bold" if dispatch_ready else ("red" if blocked else "dim")

            dt.add_row(
                Text(tid, justify="right"),
                Text(_pri_label.get(pri, "—"), style=_pri_style.get(pri, "dim")),
                Text(_diff_label.get(diff, "—"), style=_diff_style.get(diff, "dim")),
                Text(str(pts) if pts else "—", justify="right", style="bold" if pts else "dim"),
                Text(tier[:4], style=_tier_style.get(tier, "dim")),
                Text(str(tok) if tok else "—", justify="right", style="magenta" if tok else "dim"),
                Text(f"{status_icon} {status}", style=status_style),
                Text(t.get("project", "—")[:10], style="cyan"),
                Text((t.get("task_name") or "—")[:35]),
                Text(dispatch_icon, style=dispatch_style),
            )

        if not shown:
            dt.add_row(*[""] * 10)

        if built_tasks:
            remaining_built = len([t for t in sorted_tasks if t.get("status") == "built"]) - 10
            if remaining_built > 0:
                row = ["", "", "", "", "", "", Text(f"... +{remaining_built} built", style="dim"), "", "", ""]
                dt.add_row(*row)



class AccountCapacityView(LazyView):
    """Full-screen multi-account capacity view (A / B / C)."""

    def compose(self) -> ComposeResult:
        yield Static(id="cap-header")
        with Horizontal(id="cap-panels"):
            yield Static(id="cap-panel-a")
            yield Static(id="cap-panel-b")
            yield Static(id="cap-panel-c")
        yield Static(id="cap-footer")

    def load_content(self):
        accounts = get_account_capacity_display()

        # Header — highlight active account
        labels = []
        for a in accounts:
            color = {"A": "cyan", "B": "magenta", "C": "yellow"}.get(a["label"], "white")
            if a["is_active"]:
                labels.append("[bold {c}][ {l} ][/bold {c}]".format(c=color, l=a["label"]))
            else:
                labels.append("[dim]{l}[/dim]".format(l=a["label"]))

        self.query_one("#cap-header", Static).update(
            "[bold]Account Capacity[/bold]  {joined}".format(joined="  /  ".join(labels))
        )

        # Build each panel
        panel_ids = {"A": "#cap-panel-a", "B": "#cap-panel-b", "C": "#cap-panel-c"}
        colors = {"A": "cyan", "B": "magenta", "C": "yellow"}

        total_five = 0.0
        total_seven = 0.0
        healthy = 0

        for a in accounts:
            label = a["label"]
            color = colors.get(label, "white")
            panel_widget = self.query_one(panel_ids[label], Static)

            # Active indicator
            if a["is_active"]:
                title_line = "[green]●[/green] [bold {c}]Account {l}[/bold {c}] [dim]({n})[/dim]".format(
                    c=color, l=label, n=a["name"]
                )
            else:
                title_line = "[dim]○[/dim] [{c}]Account {l}[/{c}] [dim]({n})[/dim]".format(
                    c=color, l=label, n=a["name"]
                )

            # Lane
            lane_style = {"builder": "blue", "operator": "green", "overflow": "yellow"}.get(a["lane"], "dim")
            lane_line = "[dim]Lane:[/dim] [{s}]{v}[/{s}]".format(s=lane_style, v=a["lane"])

            # Repos
            repos = a.get("repos", [])
            if repos:
                repos_line = "[dim]Repos:[/dim] " + ", ".join(repos)
            else:
                repos_line = "[dim]Repos: any[/dim]"

            # Usage bars
            five_bar = self._bar(a["five_pct"])
            seven_bar = self._bar(a["seven_pct"])

            # Reset countdowns
            five_cd = _countdown(a["five_reset"]) if a["five_reset"] else "---"
            seven_cd = _countdown(a["seven_reset"]) if a["seven_reset"] else "---"

            # Data freshness
            age = a["snapshot_age_min"]
            if a["is_active"]:
                freshness = "[green]live[/green]"
            elif age < 0:
                freshness = "[dim]no data[/dim]"
            elif age < 2:
                freshness = "[green]<2m ago[/green]"
            elif age < 60:
                freshness = "[yellow]{:.0f}m ago[/yellow]".format(age)
            else:
                freshness = "[red]{:.0f}h ago[/red]".format(age / 60)

            # Track totals for footer
            try:
                total_five += float(a["five_pct"])
                total_seven += float(a["seven_pct"])
            except (ValueError, TypeError):
                pass
            try:
                if float(a["seven_pct"]) < 70:
                    healthy += 1
            except (ValueError, TypeError):
                pass

            # Compose the panel content
            lines = [
                title_line,
                lane_line,
                repos_line,
                "",
                "[bold]5h usage:[/bold]  " + five_bar,
                "[dim]  resets:[/dim] " + five_cd,
                "",
                "[bold]7d usage:[/bold]  " + seven_bar,
                "[dim]  resets:[/dim] " + seven_cd,
                "",
                "[dim]data:[/dim] " + freshness,
            ]

            border_style = "bold " + color if a["is_active"] else "dim"
            panel_widget.update(
                Panel(
                    "\n".join(lines),
                    title="[bold {c}]{l}[/bold {c}]".format(c=color, l=label),
                    border_style=border_style,
                    expand=True,
                )
            )

        # Footer: capacity health summary
        avg_five = total_five / 3
        avg_seven = total_seven / 3
        health_color = "green" if healthy >= 2 else ("yellow" if healthy >= 1 else "red")
        self.query_one("#cap-footer", Static).update(
            "[dim]Avg 5h: {five:.0f}%  Avg 7d: {seven:.0f}%  "
            "[/dim][{hc}]{h}/3 accounts healthy (<70% weekly)[/{hc}]".format(
                five=avg_five, seven=avg_seven, hc=health_color, h=healthy,
            )
        )

    @staticmethod
    def _bar(pct_val, width=20):
        # type: (Any, int) -> str
        """Render a usage bar from a percentage value."""
        try:
            pct_f = float(pct_val)
            filled = int(pct_f * width / 100)
            bar_color = "green" if pct_f < 50 else ("yellow" if pct_f < 75 else "red")
            pct_display = "{:.1f}".format(pct_f) if pct_f != int(pct_f) else str(int(pct_f))
            return "[{c}]{f}{e}[/{c}] {p}%".format(
                c=bar_color,
                f="█" * filled,
                e="░" * (width - filled),
                p=pct_display,
            )
        except (ValueError, TypeError):
            return "[dim]{e}[/dim]  ---".format(e="░" * width)



# ── DataTable widgets (scrollable) ───────────────────────────────────────────


class SessionHistoryTable(DataTable):
    BORDER_TITLE = "Session History"
    BORDER_SUBTITLE = "Tab to focus · Enter to drill down · arrows to scroll"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.add_column("When", width=9)
        self.add_column("Session", width=10)
        self.add_column("Src", width=10)
        self.add_column("Co", width=8)
        self.add_column("Project", width=12)
        self.add_column("Mdl", width=10)
        self.add_column("Dur", width=7)
        self.add_column("~5h%", width=7)
        self.add_column("Out", width=6)
        self.add_column("Cost", width=7)
        self.add_column("Directive")

    def refresh_rows(self):
        sessions = _get_session_history()
        pid_map = _build_pid_map()
        active = _active_pids()

        call_map = _get_call_data_map()
        call_by_uuid = {}
        for uuid, pid in pid_map.items():
            if pid in call_map:
                call_by_uuid[uuid] = call_map[pid]

        # Filter to current 5h window
        _, _, five_reset_ts, _ = _current_pct()
        window_start = None
        if five_reset_ts:
            try:
                reset_dt = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
                window_start = reset_dt - timedelta(hours=5)
            except Exception:
                pass
        if window_start:
            sessions = [s for s in sessions if s["last_ts"] >= window_start]

        n = len(sessions)
        self.border_title = f"Session History — {n}" if n else "Session History"

        # Get filter text from app
        filter_text = ""
        try:
            filter_text = self.app._filter_text
        except Exception:
            pass

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        with _index_lock:
            _building = _index_building
        if not sessions:
            self.add_row(
                "...", "", "", "", "", "", "", "", "", "",
                Text("building index..." if _building else "no sessions in this window", style="dim"),
            )
            return

        today = datetime.now(timezone.utc).astimezone().date()
        yesterday = today - timedelta(days=1)
        current_group = None

        for s in sessions:
            date = s["date"]
            if date == today:
                group = "Today"
            elif date == yesterday:
                group = "Yesterday"
            else:
                group = date.strftime("%b %-d")

            if group != current_group:
                sep = f"— {group} —"
                self.add_row(Text(sep, style="dim"), "", "", "", "", "", "", "", "", "", "", key=f"sep-{group}")
                current_group = group

            when_str = s["last_ts"].astimezone().strftime("%H:%M:%S")

            # Show cc-PID if we can match, otherwise short UUID
            session_display = pid_map.get(s["session_id"], s["session_id"][:10])
            is_active = s["session_id"] in pid_map and pid_map[s["session_id"]] in active

            # Apply search filter
            if filter_text:
                searchable = " ".join([
                    session_display, s.get("source", ""),
                    s.get("project", ""), s.get("directive", ""),
                    s["session_id"],
                ]).lower()
                if filter_text not in searchable:
                    continue

            mdl = _abbrev_model(s.get("model", ""))
            mdl_style = "magenta" if mdl == "opus" else ("cyan" if mdl == "sonnet" else "dim")

            pct = s["pct_str"]
            if pct == "—":
                pct_style = "dim"
            else:
                try:
                    v = float(pct.strip("+%"))
                    pct_style = "red" if v > 10 else ("yellow" if v > 5 else "green")
                except Exception:
                    pct_style = "dim"

            out_k = s["output_tokens"]
            out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)

            directive = (s["directive"] or "—")[:60]
            project = s.get("project", "—")

            src = s.get("source", "?")
            src_style = "yellow" if ("/" in src or src == "paperclip") else ("green" if src == "cli" else ("cyan" if "atlas" in src else "dim"))

            if "/" in src:
                co_name = src.split("/", 1)[0]
                co_style = "yellow"
                project = src.split("/", 1)[1]
            else:
                co_name, co_style = _project_to_company(project)

            cost = _estimate_cost(out_k, s.get("model", ""))
            cost_str = _format_cost(cost)
            cost_style = "red" if cost >= 2.0 else ("yellow" if cost >= 0.50 else "green")

            dot = "[bold green]● [/bold green]" if is_active else "  "
            self.add_row(
                Text(when_str, style="dim"),
                Text.from_markup(f"{dot}[cyan]{session_display}[/cyan]"),
                Text(src, style=src_style),
                Text(co_name, style=co_style),
                Text(project, style="dim"),
                Text(mdl, style=mdl_style),
                Text(s["dur_str"], style="dim"),
                Text(pct, style=pct_style),
                Text(out_str, style="dim", justify="right"),
                Text(cost_str, style=cost_style),
                directive,
                key=s["session_id"],
            )

            # Sub-row: tool call summary (matches Active Sessions sub-row style)
            cd = call_by_uuid.get(s["session_id"])
            if cd:
                calls_str = f"{cd['calls']} calls"
                tools_detail = cd['tools_str'][:30]
                last_tool = cd.get('recent_str', '')[:30]
            else:
                calls_str = ""
                tools_detail = ""
                last_tool = ""

            self.add_row(
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(calls_str, style="dim italic"),
                Text(""),
                Text(""),
                Text(""),
                Text(tools_detail, style="dim italic"),
                Text(last_tool, style="dim italic"),
                key=f"sub-{s['session_id']}",
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row)
        except Exception:
            pass

    def on_data_table_row_selected(self, event):
        key = event.row_key
        if key and key.value and not key.value.startswith("sep-") and not key.value.startswith("sub-"):
            session_id = key.value
            # Find directive from index
            sessions = _get_session_history()
            directive = "—"
            project = "—"
            for s in sessions:
                if s["session_id"] == session_id:
                    directive = s.get("directive", "—")
                    project = s.get("project", "—")
                    break
            self.app.push_screen(SessionDrillDown(session_id, directive, project))


class CallHistoryTable(DataTable):
    BORDER_TITLE = "Call History"
    BORDER_SUBTITLE = "Tab to focus"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.add_column("When", width=9)
        self.add_column("Session", width=10)
        self.add_column("Src", width=10)
        self.add_column("Co", width=8)
        self.add_column("Project", width=12)
        self.add_column("Mdl", width=10)
        self.add_column("#", width=4)
        self.add_column("Tools", width=20)
        self.add_column("Last Tool", width=22)
        self.add_column("5h%", width=7)
        self.add_column("Directive")

    def refresh_rows(self):
        history = _get_call_history()
        active = _active_pids()

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        if not history:
            self.add_row("...", "", "", "", "", "", "", "", "", "", Text("no data", style="dim"))
            return

        today = datetime.now(timezone.utc).astimezone().date()
        current_group = None

        for h in history:
            date = h.get("when_date")
            if date == today:
                group = "Today"
            elif date:
                group = date.strftime("%b %-d")
            else:
                group = "Unknown"

            if group != current_group:
                sep = f"— {group} —"
                self.add_row(Text(sep, style="dim"), "", "", "", "", "", "", "", "", "", "", key=f"ch-sep-{group}")
                current_group = group

            src = h["source"]
            src_style = "yellow" if ("/" in src or src == "paperclip") else ("green" if src == "cli" else "dim")

            mdl = h.get("model", "?")
            mdl_style = "magenta" if "opus" in mdl else ("cyan" if "sonnet" in mdl else "dim")

            pct = h["pct_str"]
            try:
                v = float(pct.strip("+%"))
                pct_style = "red" if v > 5 else ("yellow" if v > 2 else "green")
            except Exception:
                pct_style = "dim"

            # Green dot for active sessions
            sid = h["session"]
            dot = "[bold green]● [/bold green]" if sid in active else "  "

            project = h.get("project", "—")
            if "/" in src:
                co_name = src.split("/", 1)[0]
                co_style = "yellow"
                project = src.split("/", 1)[1]
            else:
                co_name, co_style = _project_to_company(project)

            self.add_row(
                Text(h["when"], style="dim"),
                Text.from_markup(f"{dot}[cyan]{sid[:10]}[/cyan]"),
                Text(src, style=src_style),
                Text(co_name, style=co_style),
                Text(project, style="dim"),
                Text(mdl, style=mdl_style),
                Text(str(h["calls"]), justify="right"),
                Text(h["tools_str"][:20], style="dim"),
                Text(h.get("recent_str", "—")[:22]),
                Text(pct, style=pct_style),
                Text((h["directive"] or "—")[:40]),
                key=f"ch-{h['session']}",
            )

            # Sub-row: recent tool detail
            recent = h.get("recent_str", "")
            tools = h.get("tools_str", "")
            self.add_row(
                Text(""), Text(""), Text(""), Text(""), Text(""),
                Text(""),
                Text(""),
                Text(tools[:20], style="dim italic") if tools else Text(""),
                Text(recent[:22], style="dim italic") if recent else Text(""),
                Text(""),
                Text(""),
                key=f"chsub-{h['session']}",
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row)
        except Exception:
            pass


# ── Nav Bar ──────────────────────────────────────────────────────────────────


class LeaderboardView(LazyView):
    """Multiplayer leaderboard — team competition on window scores."""

    def compose(self) -> ComposeResult:
        yield Static(id="lb-header")
        yield DataTable(id="lb-table")

    def load_content(self):
        from claude_watch_data import _get_leaderboard, _get_battlestation_config
        config = _get_battlestation_config()
        my_id = config.get("user_id", "")
        lb = _get_leaderboard(days=7)

        total_windows = sum(u.get("windows", 0) for u in lb)
        self.query_one("#lb-header", Static).update(
            f"[bold]Leaderboard — Last 7 Days[/bold]  "
            f"[dim]{len(lb)} users  {total_windows} windows[/dim]"
        )

        dt = self.query_one("#lb-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=4)
        dt.add_column("User", width=18)
        dt.add_column("Avg Stars", width=10)
        dt.add_column("Avg", width=5)
        dt.add_column("Windows", width=8)
        dt.add_column("Best", width=8)
        dt.add_column("Burn", width=6)
        dt.add_column("Ship", width=6)
        dt.add_column("Velocity", width=9)
        dt.add_column("Streak")

        for rank, u in enumerate(lb, 1):
            is_me = u["user_id"] == my_id
            ov = u["avg_overall"]
            ov_color = "green" if ov >= 4 else ("yellow" if ov >= 3 else "red")

            def _sc(v):
                return "green" if v >= 4 else ("yellow" if v >= 2.5 else "red")

            name_style = "bold cyan" if is_me else ""
            dot = "[bold green]● [/bold green]" if is_me else "  "
            streak = u.get("streak", 0)
            streak_str = f"🔥{streak}" if streak >= 3 else str(streak)

            dt.add_row(
                Text(str(rank), justify="right"),
                Text.from_markup(f"{dot}[{name_style}]{u['display_name']}[/{name_style}]") if name_style else Text.from_markup(f"{dot}{u['display_name']}"),
                Text(u["avg_stars"], style=ov_color),
                Text(f"{ov}", style=ov_color, justify="right"),
                Text(str(u["windows"]), justify="right"),
                Text(u.get("best_stars", ""), style="dim"),
                Text(f"{u['avg_burn']}", style=_sc(u['avg_burn']), justify="right"),
                Text(f"{u['avg_ship']}", style=_sc(u['avg_ship']), justify="right"),
                Text(f"{u['avg_velocity']}", style=_sc(u['avg_velocity']), justify="right"),
                Text(streak_str),
            )

        if not lb:
            dt.add_row(
                "", Text("No scores yet — complete a 5h window to appear here", style="dim"),
                "", "", "", "", "", "", "", "",
            )



# ── Cycles screens ──────────────────────────────────────────────────────────


class CyclesView(LazyView):
    """Overview of all 5-hour usage cycles (windows)."""

    BINDINGS = [
        Binding("p", "show_plan", "Plan"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="cycles-current")
        yield DataTable(id="cycles-list")

    def load_content(self):
        from claude_watch_data import (
            _get_current_cycle, _get_all_cycles, _get_cycle_sessions,
            _get_cycle_plan, _countdown, _format_cost, _current_pct,
        )

        current = _get_current_cycle()
        all_cycles = _get_all_cycles()

        # ── Current cycle panel ──
        panel = self.query_one("#cycles-current", Static)
        if current:
            five, _seven, five_reset, _sr = _current_pct()
            reset_str = _countdown(five_reset) if five_reset else "?"
            try:
                burn_pct = float(five)
            except (ValueError, TypeError):
                burn_pct = 0.0
            bar_len = 20
            filled = int(burn_pct / 100 * bar_len)
            bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
            bar_color = "green" if burn_pct < 50 else ("yellow" if burn_pct < 80 else "red")

            # Unique projects
            sessions = _get_cycle_sessions(current["cycle_id"])
            projects = sorted({s.get("project", "?") for s in sessions if s.get("project")})
            proj_str = ", ".join(projects[:5]) if projects else "\u2014"

            plan = _get_cycle_plan(current["cycle_id"])
            plan_str = "[green]plan set[/green]" if plan else "[dim]no plan[/dim]"

            panel.update(
                f"[bold]CURRENT CYCLE[/bold]  resets in {reset_str}\n"
                f"  [{bar_color}]{bar}[/{bar_color}] {burn_pct:.0f}%  "
                f"[bold]{current['session_count']}[/bold] sessions  "
                f"Projects: {proj_str}  {plan_str}  "
                f"Cost: {current['cost_str']}  "
                f"Gravity: [cyan]{current['gravity_label'] or chr(8212)}[/cyan]"
            )
        else:
            panel.update("[dim]No active cycle[/dim]")

        # ── Past cycles table ──
        dt = self.query_one("#cycles-list", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=4)
        dt.add_column("When", width=18)
        dt.add_column("Peak%", width=7)
        dt.add_column("Sessions", width=9)
        dt.add_column("Projects", width=20)
        dt.add_column("Stars", width=10)
        dt.add_column("Cost", width=8)
        dt.add_column("Gravity", width=14)

        self._cycle_map = {}  # row_key -> cycle dict
        rank = 0
        for c in all_cycles:
            if c.get("is_current"):
                continue
            rank += 1
            try:
                start_dt = datetime.fromisoformat(c["start_ts"])
                when_str = start_dt.strftime("%b %-d %-I:%M %p")
            except Exception:
                when_str = c["start_ts"][:16]

            peak = c.get("peak_five_pct", 0)
            peak_color = "green" if peak >= 80 else ("yellow" if peak >= 40 else "dim")

            # Projects from sessions
            sessions = _get_cycle_sessions(c["cycle_id"])
            projects = sorted({s.get("project", "?") for s in sessions if s.get("project")})
            proj_str = ", ".join(projects[:3]) if projects else "\u2014"

            row_key = f"cyc-{rank}"
            self._cycle_map[row_key] = c
            dt.add_row(
                Text(str(rank), justify="right"),
                Text(when_str),
                Text(f"{peak:.0f}%", style=peak_color, justify="right"),
                Text(str(c.get("session_count", 0)), justify="right"),
                Text(proj_str),
                Text(c.get("stars", ""), style="yellow"),
                Text(c.get("cost_str", "")),
                Text(c.get("gravity_label", "") or "\u2014", style="cyan"),
                key=row_key,
            )

        if rank == 0:
            dt.add_row("", Text("No past cycles recorded yet", style="dim"),
                        "", "", "", "", "", "")

    def on_data_table_row_selected(self, event):
        row_key = str(event.row_key.value) if hasattr(event.row_key, 'value') else str(event.row_key)
        cycle = self._cycle_map.get(row_key)
        if cycle:
            self.app.push_screen(CycleDetailScreen(cycle))

    def action_show_plan(self):
        self.app.push_screen(CyclePlanScreen())



class CycleDetailScreen(Screen):
    """Detailed view of a single 5-hour cycle."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, cycle):
        super().__init__()
        self.cycle = cycle

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-cycles")
        yield Static(id="cdetail-header")
        yield Static(id="cdetail-scores")
        yield Static(id="cdetail-accomplishments")
        yield DataTable(id="cdetail-sessions")
        yield Static(id="cdetail-plan")

    def on_mount(self):
        from claude_watch_data import (
            _get_cycle_sessions, _get_cycle_plan, _stars_display,
            _format_cost, _estimate_cost, _countdown,
        )

        c = self.cycle

        # ── Header ──
        try:
            start_dt = datetime.fromisoformat(c["start_ts"])
            end_dt = datetime.fromisoformat(c["end_ts"])
            start_str = start_dt.strftime("%b %-d %-I:%M %p")
            end_str = end_dt.strftime("%-I:%M %p")
        except Exception:
            start_str = c["start_ts"][:16]
            end_str = c["end_ts"][:16]

        stars = c.get("stars", "")
        acc = c.get("accomplishments", {})
        commits = len(acc.get("git_commits", []))
        peak = c.get("peak_five_pct", 0)

        self.query_one("#cdetail-header", Static).update(
            f"[bold]CYCLE:[/bold] {start_str} \u2014 {end_str}  {stars}\n"
            f"  Peak: [bold]{peak:.0f}%[/bold]  "
            f"Sessions: [bold]{c.get('session_count', 0)}[/bold]  "
            f"Cost: [bold]{c.get('cost_str', '')}[/bold]  "
            f"Commits: [bold]{commits}[/bold]"
        )

        # ── Scores ──
        ws = c.get("window_score")
        scores_panel = self.query_one("#cdetail-scores", Static)
        if ws:
            dims = []
            for dim_key in ("burn", "parallel", "ship", "breadth", "velocity"):
                val = ws.get(dim_key, 0)
                dim_stars = _stars_display(val)
                color = "green" if val >= 4 else ("yellow" if val >= 2.5 else "red")
                dims.append(f"{dim_key.capitalize()}: [{color}]{dim_stars} ({val})[/{color}]")
            scores_panel.update("  ".join(dims))
        else:
            scores_panel.update("[dim]No window score available[/dim]")

        # ── Accomplishments ──
        acc_panel = self.query_one("#cdetail-accomplishments", Static)
        files_edited = len(acc.get("files_edited", []))
        files_created = len(acc.get("files_created", []))
        errors = acc.get("errors", 0)
        skills = acc.get("skills", [])
        turns = acc.get("turn_count", 0)

        acc_lines = []
        err_style = "red" if errors else "dim"
        acc_lines.append(
            f"  Files edited: [bold]{files_edited}[/bold]  "
            f"Files created: [bold]{files_created}[/bold]  "
            f"Commits: [bold]{commits}[/bold]  "
            f"Errors: [bold {err_style}]{errors}[/bold {err_style}]  "
            f"Turns: [bold]{turns}[/bold]"
        )
        if skills:
            acc_lines.append(f"  Skills: {', '.join(skills[:10])}")
        acc_panel.update("\n".join(acc_lines))

        # ── Sessions DataTable ──
        dt = self.query_one("#cdetail-sessions", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Session", width=12)
        dt.add_column("Project", width=18)
        dt.add_column("Duration", width=10)
        dt.add_column("Tokens", width=10)
        dt.add_column("Cost", width=8)
        dt.add_column("Directive", width=30)

        sessions = _get_cycle_sessions(c["cycle_id"])
        self._session_map = {}  # row_key -> session dict

        for i, s in enumerate(sessions):
            sid = s.get("session_id", "?")
            short_sid = sid[:10] if len(sid) > 10 else sid
            project = s.get("project", "\u2014")
            tokens = s.get("output_tokens", 0) or 0
            model = s.get("model", "")
            cost = _estimate_cost(tokens, model)
            directive = s.get("directive", "") or ""

            # Duration
            try:
                first = s.get("first_ts")
                last = s.get("last_ts")
                if first and last:
                    if not isinstance(first, datetime):
                        first = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
                    if not isinstance(last, datetime):
                        last = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    dur_secs = int((last - first).total_seconds())
                    dur_m = dur_secs // 60
                    dur_str = f"{dur_m}m" if dur_m < 60 else f"{dur_m // 60}h{dur_m % 60:02d}m"
                else:
                    dur_str = "\u2014"
            except Exception:
                dur_str = "\u2014"

            tok_str = f"{tokens // 1000}k" if tokens >= 1000 else str(tokens)

            row_key = f"csess-{i}"
            self._session_map[row_key] = s
            dt.add_row(
                Text(short_sid, style="cyan"),
                Text(project),
                Text(dur_str, justify="right"),
                Text(tok_str, justify="right"),
                Text(_format_cost(cost)),
                Text(directive[:30], style="dim"),
                key=row_key,
            )

        if not sessions:
            dt.add_row(
                Text("\u2014", style="dim"), Text("No sessions in this cycle", style="dim"),
                Text(""), Text(""), Text(""), Text(""),
            )

        # ── Plan ──
        plan_panel = self.query_one("#cdetail-plan", Static)
        plan = _get_cycle_plan(c["cycle_id"])
        if plan:
            tasks = plan.get("tasks", [])
            if tasks:
                lines = ["[bold]PLAN[/bold]"]
                for t in tasks:
                    status = t.get("status", "pending")
                    icon = "\u2713" if status == "done" else ("\u2298" if status == "skipped" else "\u25cb")
                    color = "green" if status == "done" else ("dim" if status == "skipped" else "white")
                    lines.append(
                        f"  [{color}]{icon} {t.get('title', '?')}  "
                        f"({t.get('project', '?')})  "
                        f"est:{t.get('est_pct', 0):.0f}%  "
                        f"act:{t.get('act_pct', 0):.0f}%[/{color}]"
                    )
                plan_panel.update("\n".join(lines))
            else:
                plan_panel.update("[dim]Plan exists but has no tasks[/dim]")
        else:
            plan_panel.update("[dim]No plan for this cycle[/dim]")

    def on_data_table_row_selected(self, event):
        row_key = str(event.row_key.value) if hasattr(event.row_key, 'value') else str(event.row_key)
        s = self._session_map.get(row_key)
        if s:
            self.app.push_screen(SessionDrillDown(
                session_id=s.get("session_id", ""),
                directive=s.get("directive", ""),
                project=s.get("project", "\u2014"),
            ))

    def action_pop_screen(self):
        self.app.pop_screen()


class CyclePlanScreen(Screen):
    """Plan tasks for the current 5-hour cycle."""

    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
        Binding("a", "add_task", "Add"),
        Binding("d", "done_task", "Done"),
        Binding("s", "skip_task", "Skip"),
    ]

    def compose(self) -> ComposeResult:
        yield NavBar(active="nav-cycles")
        yield Static(id="cplan-header")
        yield DataTable(id="cplan-tasks")
        yield DataTable(id="cplan-available")

    def on_mount(self):
        self._load_and_render()

    def _load_and_render(self):
        from claude_watch_data import (
            _get_current_cycle, _get_cycle_plan, _save_cycle_plan,
            _get_plannable_tasks, _current_pct, _format_cost,
        )

        current = _get_current_cycle()
        header = self.query_one("#cplan-header", Static)

        if not current:
            header.update("[dim]No active cycle \u2014 start a session to create one[/dim]")
            return

        self._cycle_id = current["cycle_id"]
        plan = _get_cycle_plan(self._cycle_id)
        if not plan:
            plan = {"cycle_id": self._cycle_id, "tasks": [], "budget_pct": 100.0}
        self._plan = plan

        # Budget calculation
        five, _seven, _fr, _sr = _current_pct()
        try:
            burned = float(five)
        except (ValueError, TypeError):
            burned = 0.0

        allocated = sum(t.get("est_pct", 0) for t in plan.get("tasks", []))
        remaining = 100.0 - burned
        plan_remaining = remaining - allocated

        bar_len = 30
        burned_chars = int(burned / 100 * bar_len)
        alloc_chars = int(allocated / 100 * bar_len)
        free_chars = bar_len - burned_chars - alloc_chars
        if free_chars < 0:
            free_chars = 0
            alloc_chars = bar_len - burned_chars

        _full = '\u2588'
        _light = '\u2591'
        bar = (
            f"[red]{_full * burned_chars}[/red]"
            f"[yellow]{_full * alloc_chars}[/yellow]"
            f"[green]{_light * free_chars}[/green]"
        )

        header.update(
            f"[bold]CYCLE PLAN[/bold]  {bar}  "
            f"Burned: [red]{burned:.0f}%[/red]  "
            f"Allocated: [yellow]{allocated:.0f}%[/yellow]  "
            f"Free: [green]{plan_remaining:.0f}%[/green]\n"
            f"  [dim](a=add task  d=mark done  s=skip)[/dim]"
        )

        # ── Planned tasks table ──
        pt = self.query_one("#cplan-tasks", DataTable)
        pt.clear(columns=True)
        pt.cursor_type = "row"
        pt.zebra_stripes = True
        pt.add_column("#", width=4)
        pt.add_column("Status", width=8)
        pt.add_column("Task", width=35)
        pt.add_column("Project", width=15)
        pt.add_column("Est%", width=6)
        pt.add_column("Act%", width=6)

        tasks = plan.get("tasks", [])
        for i, t in enumerate(tasks):
            status = t.get("status", "pending")
            icon = "\u2713 done" if status == "done" else ("\u2298 skip" if status == "skipped" else "\u25cb pend")
            color = "green" if status == "done" else ("dim" if status == "skipped" else "white")

            pt.add_row(
                Text(str(i + 1), justify="right"),
                Text(icon, style=color),
                Text(t.get("title", "?")[:35]),
                Text(t.get("project", "?")),
                Text(f"{t.get('est_pct', 0):.0f}%", justify="right"),
                Text(f"{t.get('act_pct', 0):.0f}%", justify="right"),
                key=f"ptask-{i}",
            )

        if not tasks:
            pt.add_row("", Text("No tasks planned \u2014 press 'a' on available tasks below", style="dim"),
                        "", "", "", "")

        # ── Available tasks table ──
        at = self.query_one("#cplan-available", DataTable)
        at.clear(columns=True)
        at.cursor_type = "row"
        at.zebra_stripes = True
        at.add_column("ID", width=6)
        at.add_column("Title", width=35)
        at.add_column("Project", width=15)
        at.add_column("~kT", width=6)
        at.add_column("Est%", width=6)
        at.add_column("Tier", width=6)
        at.add_column("Pri", width=5)

        available = _get_plannable_tasks()
        # Filter out already-planned task IDs
        planned_ids = {t.get("id") for t in tasks}
        self._available_tasks = []
        for t in available:
            if t.get("id") in planned_ids:
                continue
            self._available_tasks.append(t)
            at.add_row(
                Text(str(t.get("id", "?"))[:6]),
                Text(t.get("title", "?")[:35]),
                Text(t.get("project", "?")),
                Text(str(t.get("est_tokens_k", "?"))),
                Text(f"{t.get('est_pct', 0):.0f}%", justify="right"),
                Text(str(t.get("tier", "?"))),
                Text(str(t.get("priority", "?"))),
                key=f"avail-{t.get('id', '')}",
            )

        if not self._available_tasks:
            at.add_row("", Text("No ready tasks in Build Tracker", style="dim"),
                        "", "", "", "", "")

    def action_add_task(self):
        from claude_watch_data import _save_cycle_plan
        at = self.query_one("#cplan-available", DataTable)
        if not at.row_count or not hasattr(self, '_available_tasks') or not self._available_tasks:
            return

        # Find the task by cursor position
        try:
            idx = at.cursor_row
            if idx >= len(self._available_tasks):
                return
            task = self._available_tasks[idx]
        except (IndexError, AttributeError):
            return

        new_entry = {
            "id": task.get("id"),
            "title": task.get("title", "?"),
            "project": task.get("project", "?"),
            "est_pct": task.get("est_pct", 0),
            "status": "pending",
            "act_pct": 0,
        }
        self._plan.setdefault("tasks", []).append(new_entry)
        _save_cycle_plan(self._plan)
        self._load_and_render()

    def action_done_task(self):
        from claude_watch_data import _save_cycle_plan
        pt = self.query_one("#cplan-tasks", DataTable)
        tasks = self._plan.get("tasks", [])
        if not pt.row_count or not tasks:
            return
        try:
            idx = pt.cursor_row
            if idx >= len(tasks):
                return
            tasks[idx]["status"] = "done"
            _save_cycle_plan(self._plan)
            self._load_and_render()
        except (IndexError, AttributeError):
            return

    def action_skip_task(self):
        from claude_watch_data import _save_cycle_plan
        pt = self.query_one("#cplan-tasks", DataTable)
        tasks = self._plan.get("tasks", [])
        if not pt.row_count or not tasks:
            return
        try:
            idx = pt.cursor_row
            if idx >= len(tasks):
                return
            tasks[idx]["status"] = "skipped"
            _save_cycle_plan(self._plan)
            self._load_and_render()
        except (IndexError, AttributeError):
            return

    def action_pop_screen(self):
        self.app.pop_screen()


# ── App ──────────────────────────────────────────────────────────────────────


def _render_pie_chart(sessions, width=30, height=15):
    """Render an ASCII pie chart using Unicode blocks."""
    if not sessions:
        return "[dim]No data[/dim]"

    # Use output_tokens for proportions (more meaningful than % which are all similar)
    total_tokens = sum(s.get("output_tokens", 0) or 1 for s in sessions)

    # Build angle ranges for each session
    slices = []  # (start_angle, end_angle, color, label)
    current_angle = -math.pi / 2  # Start from top (12 o'clock)
    for s in sessions:
        tokens = s.get("output_tokens", 0) or 1
        sweep = 2 * math.pi * tokens / total_tokens
        slices.append((current_angle, current_angle + sweep, s["color"], s.get("directive", "")[:15] or s["session_id"][:10]))
        current_angle += sweep

    # Render circle
    cx, cy = width / 2, height / 2
    # Account for terminal character aspect ratio (~2:1 width:height)
    rx = width / 2 - 1  # radius x
    ry = height / 2 - 0.5  # radius y

    lines = []
    for row in range(height):
        line_chars = []
        for col in range(width):
            # Normalize to unit circle
            dx = (col - cx) / rx if rx else 0
            dy = (row - cy) / ry if ry else 0
            dist = math.sqrt(dx * dx + dy * dy)

            if dist > 1.0:
                line_chars.append(" ")
                continue

            # Calculate angle
            angle = math.atan2(dy, dx)

            # Find which slice this angle belongs to
            color = "white"
            for start, end, c, _ in slices:
                # Normalize angles
                a = angle
                s_a = start
                # Handle wrap-around
                while a < s_a:
                    a += 2 * math.pi
                if s_a <= a < end:
                    color = c
                    break

            line_chars.append(f"[{color}]\u2588[/{color}]")

        lines.append("".join(line_chars))

    return "\n".join(lines)


class TokenAttributionScreen(Screen):
    """Full-screen token attribution breakdown."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        from textual.widgets import Footer
        yield NavBar(active="nav-dashboard")
        yield Static(id="attr-header")
        with Horizontal(id="attr-chart-row"):
            yield Static(id="attr-pie")
            yield Static(id="attr-legend")
        yield DataTable(id="attr-table")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#attr-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "\u25a0", "Session", "Directive", "Time",
            "% Used", "Out Tokens", "Model", "Tools"
        )
        self.refresh_data()

    def refresh_data(self):
        data = _get_token_attribution()
        header = self.query_one("#attr-header", Static)
        pie_widget = self.query_one("#attr-pie", Static)
        legend_widget = self.query_one("#attr-legend", Static)

        if not data or not data.get("sessions"):
            header.update("[bold]Token Attribution[/bold] \u2014 No data yet")
            pie_widget.update("")
            legend_widget.update("")
            return

        total = data["total_used_pct"]
        unaccounted = data.get("unaccounted_pct", 0)

        try:
            bar_width = max(20, self.size.width - 6)
        except Exception:
            bar_width = 60

        bar_chars = []
        sessions = data["sessions"]
        for s in sessions:
            pct = s["pct_used"]
            if pct < 0.3:
                continue
            cols = max(1, int(pct / max(total, 1) * bar_width))
            color = s["color"]
            label = f"{pct:.0f}%"
            segment = label.center(cols) if cols >= len(label) + 2 else "\u2588" * cols
            bar_chars.append(f"[bold white on {color}]{segment}[/]")

        if unaccounted > 0.5:
            cols = max(1, int(unaccounted / max(total, 1) * bar_width))
            segment = f"{unaccounted:.0f}%".center(cols) if cols >= 6 else "\u2591" * cols
            bar_chars.append(f"[dim]{segment}[/dim]")

        bar = "".join(bar_chars)
        header.update(f"[bold]Who Ate My {total:.0f}%?[/bold]  5h rolling window\n{bar}")

        # Render pie chart
        pie_text = _render_pie_chart(sessions)
        pie_widget.update(pie_text)

        # Render legend
        total_tokens = sum(s.get("output_tokens", 0) or 0 for s in sessions)
        legend_lines = []
        for s in sessions:
            color = s["color"]
            directive = s.get("directive", "")[:25] if s.get("directive") else s["session_id"][:12]
            out_tokens = s.get("output_tokens", 0) or 0
            if out_tokens >= 1_000_000:
                tok_str = f"{out_tokens / 1_000_000:.1f}M"
            elif out_tokens >= 1_000:
                tok_str = f"{out_tokens / 1_000:.0f}K"
            else:
                tok_str = str(out_tokens)
            pct_of_total = (out_tokens / total_tokens * 100) if total_tokens > 0 else 0
            legend_lines.append(
                f"[{color}]\u2588\u2588[/{color}] {directive:<25s} {tok_str:>6s} {pct_of_total:>4.0f}%"
            )
        legend_widget.update("\n".join(legend_lines))

        # Populate table
        table = self.query_one("#attr-table", DataTable)
        table.clear()

        for s in sessions:
            first = s["first_ts"].astimezone().strftime("%H:%M")
            last = s["last_ts"].astimezone().strftime("%H:%M")
            time_range = f"{first}\u2013{last}"

            color = s["color"]
            color_block = Text("\u2588\u2588", style=color)
            directive = s["directive"][:30] if s["directive"] else "\u2014"
            pct_str = f"{s['pct_used']:.1f}%"
            tokens = f"{s['output_tokens']:,}" if s["output_tokens"] else "\u2014"
            model = s.get("model", "?")
            tools = str(s["tool_count"])
            sid = s["session_id"][:12]

            table.add_row(color_block, sid, directive, time_range, pct_str, tokens, model, tools)

        if unaccounted > 0.5:
            table.add_row(
                Text("\u2591\u2591", style="dim"),
                "\u2014", "Rolled out of window", "\u2014",
                f"{unaccounted:.1f}%", "\u2014", "\u2014", "\u2014"
            )


class ClaudeWatchApp(App):
    CSS_PATH = "claude_watch_tui.tcss"
    TITLE = "claude-watch"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("e", "export_csv", "Export CSV"),
        Binding("u", "show_usage", "Usage"),
        Binding("m", "show_mcp", "MCP"),
        Binding("s", "show_session_tasks", "Cycle"),
        Binding("p", "show_project_board", "Board"),
        Binding("l", "show_leaderboard", "Leaderboard"),
        Binding("a", "toggle_accounts", "Accounts"),
        Binding("c", "show_capacity", "Capacity"),
        Binding("h", "toggle_health", "Health"),
        Binding("y", "show_cycles", "Cycles"),
        Binding("w", "show_attribution", "Who?"),
        Binding("slash", "start_search", "Search"),
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
        Binding("R", "reload_build", "Reload", show=False),
    ]

    _filter_text = ""
    _pending_reload = False
    _revert_cooldown_until = 0.0

    def compose(self) -> ComposeResult:
        from textual.widgets import Input, Footer
        yield ReloadBanner(id="reload-banner")
        yield NavBar(id="nav-bar")
        with ContentSwitcher(initial="view-dashboard", id="content-switcher"):
            with ScrollableContainer(id="view-dashboard"):
                yield TokenHeader(id="header")
                yield AccountCapacityPanel(id="account-capacity")
                yield BurndownChart(id="burndown")
                yield TokenAttributionPanel(id="attribution")
                yield Input(placeholder="Search sessions (ccid, project, directive)...", id="search-input")
                yield UrgentAlerts(id="urgent")
                yield SystemHealthPanel(id="system-health")
                yield ActiveSessionsTable(id="active-sessions")
                yield SessionNarrativePanel(id="session-narrative")
                yield SessionHistoryTable(id="session-history")
                yield DrainPanel(id="drain")
                with Horizontal(id="feed-row"):
                    yield ToolFrequency(id="tool-freq")
                    yield SkillsPanel(id="skills")
                    yield AgentsPanel(id="agents")
            yield UsageMetricsView(id="view-usage")
            yield MCPStatsView(id="view-mcp")
            yield SessionTasksView(id="view-sessions")
            yield ProjectBoardView(id="view-projects")
            yield AccountCapacityView(id="view-capacity")
            yield LeaderboardView(id="view-leaderboard")
            yield CyclesView(id="view-cycles")
        yield Footer()

    def switch_view(self, view_id: str) -> None:
        """Switch content view and update NavBar highlight."""
        switcher = self.query_one("#content-switcher", ContentSwitcher)
        switcher.current = view_id
        # Lazy-load on first visit
        if view_id != "view-dashboard":
            view = self.query_one(f"#{view_id}")
            if hasattr(view, '_loaded') and not view._loaded:
                view._loaded = True
                view.load_content()
        # Update NavBar active button
        nav_map = {
            "view-dashboard": "nav-dashboard",
            "view-usage": "nav-usage",
            "view-mcp": "nav-mcp",
            "view-sessions": "nav-sessions",
            "view-projects": "nav-projects",
            "view-capacity": "nav-capacity",
            "view-leaderboard": "nav-leaderboard",
            "view-cycles": "nav-cycles",
        }
        active_nav = nav_map.get(view_id, "")
        for btn in self.query("#nav-bar Button"):
            btn.variant = "primary" if btn.id == active_nav else "default"

    def on_mount(self):
        _load_index()
        _backup_working_files()
        self.build_index()
        # Hide search input, account capacity, and header (merged into burndown)
        self.query_one("#search-input").display = False
        self.query_one("#account-capacity").display = False
        self.query_one("#header").display = False
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()
        # Start hot-reload watcher in background
        import threading
        threading.Thread(target=_start_hot_reload_watcher, args=(self,), daemon=True).start()

    _RESTART_EXIT_CODE = 42

    def _trigger_reload(self):
        """Legacy — redirects to safe reload flow."""
        self._signal_files_changed()

    def _signal_files_changed(self):
        """Called from watcher thread when source files change."""
        import time as _time
        if _time.time() < self._revert_cooldown_until:
            return
        self._pending_reload = True
        try:
            self.query_one("#reload-banner", ReloadBanner).show_pending()
        except Exception:
            pass

    def action_reload_build(self):
        """Validate new code and restart if safe, or revert if broken."""
        if not self._pending_reload:
            return

        import subprocess, sys
        project_dir = str(Path(__file__).resolve().parent)

        result = subprocess.run(
            [sys.executable, "-c", "import claude_watch_data; import claude_watch_tui"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode == 0:
            self._pending_reload = False
            _backup_working_files()
            self.notify("Reloading...", severity="warning", timeout=1)
            self.set_timer(0.5, lambda: self.exit(return_code=self._RESTART_EXIT_CODE))
        else:
            import time as _time
            error_msg = result.stderr or result.stdout or "Unknown import error"
            self._log_build_error(error_msg)
            restored = _restore_backup_files()
            try:
                banner = self.query_one("#reload-banner", ReloadBanner)
                if restored:
                    banner.show_reverted(error_msg)
                    self.notify("Build broken \u2014 reverted to last working version", severity="error", timeout=10)
                else:
                    banner.show_reverted("No backup available!")
                    self.notify("Build broken \u2014 no backup to revert to!", severity="error", timeout=10)
            except Exception:
                pass
            self._pending_reload = False
            self._revert_cooldown_until = _time.time() + 5

    def _log_build_error(self, error_msg):
        """Log build error for debugging."""
        try:
            log_dir = Path.home() / ".claude" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "claude-watch-build-errors.log"
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a") as f:
                f.write(f"\n{'=' * 60}\n")
                f.write(f"[{timestamp}] Build validation failed\n")
                f.write(f"{'=' * 60}\n")
                f.write(error_msg)
                f.write("\n")
        except Exception:
            pass

    def build_index(self):
        import threading
        t = threading.Thread(target=_build_or_update_index, daemon=True)
        t.start()

    def refresh_data(self):
        switcher = self.query_one("#content-switcher", ContentSwitcher)

        if switcher.current == "view-dashboard":
            five, seven, fr, sr = _current_pct()
            self.query_one("#header", TokenHeader).update_content(five, seven, fr, sr)
            acct = self.query_one("#account-capacity", AccountCapacityPanel)
            if acct.display:
                acct.update_content()
            self.query_one("#burndown", BurndownChart).update_content()
            self.query_one("#attribution", TokenAttributionPanel).update_content()
            self.query_one("#urgent", UrgentAlerts).update_content()
            self.query_one("#active-sessions", ActiveSessionsTable).refresh_rows()
            self.query_one("#session-narrative", SessionNarrativePanel).update_content()
            self.query_one("#system-health", SystemHealthPanel).update_content()
            self.query_one("#session-history", SessionHistoryTable).refresh_rows()
            self.query_one("#tool-freq", ToolFrequency).update_content()
            self.query_one("#skills", SkillsPanel).update_content()
            self.query_one("#agents", AgentsPanel).update_content()
            self.query_one("#drain", DrainPanel).update_content()
        else:
            try:
                view = self.query_one(f"#{switcher.current}")
                if hasattr(view, 'refresh_content'):
                    view.refresh_content()
            except Exception:
                pass

        # Auto-score completed windows (keep unconditional)
        from claude_watch_data import _check_and_score_completed_window
        new_score = _check_and_score_completed_window()
        if new_score:
            stars = new_score.get("stars", "")
            ov = new_score.get("overall", 0)
            self.notify(f"Window scored: {stars} ({ov})", severity="information", timeout=10)

        # System notifications on spike (keep unconditional)
        try:
            five_f, seven_f = [float(x) for x in _current_pct()[:2]]
            burndown = _get_burndown_data()
            burn_rate = burndown.get("current_rate") if burndown else None
            check_and_notify(five_f, seven_f, burn_rate)
        except (ValueError, TypeError):
            pass

    def action_force_refresh(self):
        self.build_index()
        switcher = self.query_one("#content-switcher", ContentSwitcher)
        if switcher.current == "view-dashboard":
            self.refresh_data()
        else:
            view = self.query_one(f"#{switcher.current}")
            if hasattr(view, '_loaded'):
                view._loaded = False
                view.load_content()
                view._loaded = True

    def action_export_csv(self):
        filename = os.path.expanduser(
            "~/Downloads/claude-watch-{}.csv".format(
                datetime.now().strftime("%Y%m%d-%H%M%S")
            )
        )
        try:
            count = export_session_history_csv(filename)
            self.notify(
                "{} rows exported to {}".format(count, filename),
                severity="information",
                timeout=5,
            )
        except Exception as exc:
            self.notify(
                "Export failed: {}".format(exc),
                severity="error",
                timeout=5,
            )

    def action_show_usage(self):
        self.switch_view("view-usage")

    def action_show_mcp(self):
        self.switch_view("view-mcp")

    def action_show_session_tasks(self):
        self.switch_view("view-sessions")

    def action_show_project_board(self):
        self.switch_view("view-projects")

    def action_show_leaderboard(self):
        self.switch_view("view-leaderboard")

    def action_show_capacity(self):
        self.switch_view("view-capacity")

    def action_show_attribution(self):
        self.push_screen(TokenAttributionScreen())

    def action_show_cycles(self):
        self.switch_view("view-cycles")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_map = {
            "nav-dashboard": "view-dashboard",
            "nav-sessions": "view-sessions",
            "nav-projects": "view-projects",
            "nav-leaderboard": "view-leaderboard",
            "nav-usage": "view-usage",
            "nav-mcp": "view-mcp",
            "nav-cycles": "view-cycles",
        }
        btn_id = event.button.id or ""
        if not btn_id.startswith("nav-"):
            return
        # Pop to root first (handles nav from detail screens)
        while len(self.screen_stack) > 1:
            self.pop_screen()
        if btn_id in btn_map:
            self.switch_view(btn_map[btn_id])
        elif btn_id == "nav-health":
            self.switch_view("view-dashboard")
            self.action_toggle_health()

    def action_toggle_accounts(self):
        acct = self.query_one("#account-capacity", AccountCapacityPanel)
        if acct.display:
            acct.display = False
        else:
            acct.display = True
            acct.update_content()

    def action_toggle_health(self):
        self.push_screen(HealthScreen())

    def action_start_search(self):
        from textual.widgets import Input
        search = self.query_one("#search-input", Input)
        search.display = True
        search.focus()

    def on_input_changed(self, event):
        if event.input.id == "search-input":
            self._filter_text = event.value.strip().lower()
            self.query_one("#session-history", SessionHistoryTable).refresh_rows()

    def on_input_submitted(self, event):
        if event.input.id == "search-input":
            # Hide search if empty, otherwise keep filtering
            if not event.value.strip():
                event.input.display = False
                self._filter_text = ""
                self.query_one("#session-history", SessionHistoryTable).refresh_rows()

    def on_key(self, event):
        switcher = self.query_one("#content-switcher", ContentSwitcher)
        if event.key == "escape":
            from textual.widgets import Input
            search = self.query_one("#search-input", Input)
            if search.display:
                search.display = False
                search.value = ""
                self._filter_text = ""
                self.query_one("#session-history", SessionHistoryTable).refresh_rows()
                event.prevent_default()
                event.stop()
            elif switcher.current != "view-dashboard":
                self.switch_view("view-dashboard")
                event.prevent_default()
                event.stop()
        elif event.key == "p" and switcher.current == "view-cycles":
            self.push_screen(CyclePlanScreen())
            event.prevent_default()
            event.stop()


def _cli_session_lookup(args):
    """Handle --session CLI lookup."""
    import sys
    _load_index()
    # Force index build synchronously for CLI
    _build_or_update_index()

    entry = lookup_by_ccid(args.session)
    if not entry:
        print(json.dumps({"error": f"Session '{args.session}' not found"}), file=sys.stderr)
        sys.exit(1)

    if args.context:
        # Build context packet
        turns = _get_session_turns(entry["session_id"])
        last_turns = turns[-5:] if turns else []
        packet = {
            "ccid": entry.get("ccid", "?"),
            "uuid": entry["session_id"],
            "directive": entry.get("gravity") or entry.get("directive", "—"),
            "project": entry.get("project", "—"),
            "first_ts": entry.get("first_ts"),
            "last_ts": entry.get("last_ts"),
            "output_tokens": entry.get("output_tokens", 0),
            "model": entry.get("model", "?"),
            "source": entry.get("source", "?"),
            "transcript_path": str(Path(entry.get("project_dir", "")) / f"{entry['session_id']}.jsonl"),
            "accomplishments": entry.get("accomplishments", {}),
            "last_turns": [
                {"turn": t["turn"], "prompt": t["prompt"], "tools": t["tools"], "tokens_out": t["tokens_out"]}
                for t in last_turns
            ],
        }
        print(json.dumps(packet, indent=2))
    else:
        # Basic lookup
        out = {
            "ccid": entry.get("ccid", "?"),
            "uuid": entry["session_id"],
            "directive": entry.get("gravity") or entry.get("directive", "—"),
            "project": entry.get("project", "—"),
            "first_ts": entry.get("first_ts"),
            "last_ts": entry.get("last_ts"),
            "output_tokens": entry.get("output_tokens", 0),
            "model": entry.get("model", "?"),
            "source": entry.get("source", "?"),
            "transcript_path": str(Path(entry.get("project_dir", "")) / f"{entry['session_id']}.jsonl"),
        }
        print(json.dumps(out, indent=2))


def _cli_list_sessions(args):
    """Handle --list CLI command."""
    _load_index()
    _build_or_update_index()

    from claude_watch_data import _get_session_history
    sessions = _get_session_history()[:20]

    if not sys.stdout.isatty():
        # JSON output for piping
        result = []
        for s in sessions:
            result.append({
                "session_id": s["session_id"],
                "project": s.get("project", "—"),
                "directive": s.get("directive", "—"),
                "source": s.get("source", "?"),
                "output_tokens": s.get("output_tokens", 0),
                "duration": s.get("dur_str", "?"),
            })
        print(json.dumps(result, indent=2))
    else:
        # Formatted table for terminal
        fmt = "{:<10} {:<10} {:<12} {:<8} {:<7} {}"
        print(fmt.format("Session", "Source", "Project", "Dur", "Out", "Directive"))
        print("-" * 80)
        from claude_watch_data import _build_pid_map
        pid_map = _build_pid_map()
        for s in sessions:
            sid = pid_map.get(s["session_id"], s["session_id"][:10])
            out_k = s["output_tokens"]
            out_str = f"{out_k/1000:.1f}k" if out_k >= 1000 else str(out_k)
            directive = (s.get("directive") or "—")[:40]
            print(fmt.format(
                sid, s.get("source", "?"), s.get("project", "—"),
                s.get("dur_str", "?"), out_str, directive,
            ))


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="claude-watch — Claude Code token monitor")
    parser.add_argument("-s", "--session", help="Look up session by CCID or UUID prefix")
    parser.add_argument("-l", "--list", action="store_true", help="List recent sessions")
    parser.add_argument("--context", action="store_true", help="Include resume context (with --session)")
    args = parser.parse_args()

    if args.session:
        _cli_session_lookup(args)
        return
    if args.list:
        _cli_list_sessions(args)
        return

    while True:
        app = ClaudeWatchApp()
        result = app.run()
        if result != ClaudeWatchApp._RESTART_EXIT_CODE:
            break


if __name__ == "__main__":
    main()
