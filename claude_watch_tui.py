#!/usr/bin/env python3
"""
claude-watch TUI — Textual-based interactive dashboard for Claude Code token monitoring.
Scrollable panels, keyboard navigation, no dead space.
"""

import json
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
from textual.widgets import Button, DataTable, Static

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
    _get_call_data_map,
    _get_call_history,
    _get_daily_usage,
    _get_mcp_stats,
    _get_pid_cpu,
    _get_session_history,
    _get_session_turns,
    _get_system_health,
    _get_usage_metrics,
    _index_building,
    _index_cache,
    _load_index,
    _load_ledger,
    _shorten_tool,
    focus_session_terminal,
    lookup_by_ccid,
    make_drain_panel,
    make_header,
    make_sessions_panel,
    make_skills_panel,
    make_tool_stats,
)

def _start_hot_reload_watcher(app):
    # type: (Any) -> None
    """Watch *.py files in the same directory. On any mtime change, restart the process."""
    watch_dir = Path(__file__).parent

    def _snapshot():
        # type: () -> Dict[Path, float]
        result = {}
        for p in watch_dir.glob("*.py"):
            try:
                result[p] = p.stat().st_mtime
            except Exception:
                pass
        return result

    mtimes = _snapshot()
    while True:
        time.sleep(2)
        current = _snapshot()
        if current != mtimes:
            app.call_from_thread(app._trigger_reload)
            return


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
        """Rebuild the table from live session data."""
        from claude_watch_data import _detect_source

        sessions = _active_sessions()
        entries = _load_ledger(last_n=500)
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now()

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        if not sessions:
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

            self.add_row(
                Text(start_str, style="dim"),
                Text.from_markup(f"[bold green]● [/bold green][cyan]{sid}[/cyan]"),
                Text(source, style=src_color),
                Text(co_name, style=co_style),
                Text(project, style="dim"),
                Text(mdl, style=mdl_style),
                Text(age, style="dim"),
                Text(delta, style=color),
                Text(directive),
                key=f"active-{pid}",
            )

            # Sub-row: live call state
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
            elif cpu > 20:
                state_txt = Text("thinking...", style="bold yellow")
            elif secs_since is not None and secs_since < 120:
                state_txt = Text(f"~ {tool_name[:12]}", style="dim")
            else:
                state_txt = Text("idle", style="dim")

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


class DrainPanel(Static):
    def update_content(self):
        self.update(make_drain_panel())


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

        # Build right-side lines (aligned with chart rows)
        r = [
            f"  [bold {used_color}]{used_pct:.0f}% Used[/bold {used_color}]  [bold {left_color}]{remaining:.0f}% Left[/bold {left_color}]",
            f"  [bold]5h[/bold] {mini_bar(five)} {float(five):.0f}%  [dim]resets {reset_str}[/dim]",
            f"  [bold]7d[/bold] {mini_bar(seven)} {float(seven):.0f}%  [dim]{_reset_day(sr)[:10]}[/dim]",
            f"  [{acct_color}]Acct {label}[/{acct_color}]: {name} [dim]({lane})[/dim]",
            f"  {pace_str}",
            f"  {verdict}",
            f"  {details_line}",
        ]

        lines = [
            f"100%│{rows[0]}│{r[0]}",
            f"    │{rows[1]}│{r[1]}",
            f"  0%│{rows[2]}│{r[2]}",
            f"    └{border_str}┘{r[3]}",
            f"     [dim]{axis_str}[/dim]{r[4]}",
            r[5],
            r[6],
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

            mem_str = f"{mem/1024:.1f}GB" if mem >= 1024 else f"{mem}MB"
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
                mem_str,
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
        yield Static(
            f"[bold]Session:[/bold] {self.session_id}  "
            f"[bold]Project:[/bold] {self.session_project}  "
            f"[bold]Directive:[/bold] {self.session_directive}  "
            "[dim](Escape=back  t=toggle view)[/dim]",
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


class UsageMetricsScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="metrics-header")
        yield DailySparklinePanel(id="metrics-sparkline")
        yield DataTable(id="metrics-table")
        yield Static(id="metrics-summary")

    def on_mount(self):
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
            f"[dim]Account 7d: {seven}%  (Escape to go back)[/dim]"
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

    def action_pop_screen(self):
        self.app.pop_screen()


class MCPStatsScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="mcp-header")
        with Horizontal(id="mcp-body"):
            yield DataTable(id="mcp-servers-table")
            yield DataTable(id="mcp-actions-table")

    def on_mount(self):
        from claude_watch_data import _get_mcp_stats
        stats = _get_mcp_stats(days=7)

        self.query_one("#mcp-header", Static).update(
            f"[bold]MCP Tool Usage — last 7 days[/bold]  "
            f"[dim]Total calls: {stats['total_calls']}  "
            f"Sessions using MCP: {stats['sessions_with_mcp']}  "
            f"(Escape / q to go back)[/dim]"
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

    def action_pop_screen(self):
        self.app.pop_screen()


class SessionTasksScreen(Screen):
    """Session Monitor — execution-layer task log."""
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="stasks-header")
        yield DataTable(id="stasks-table")

    def on_mount(self):
        from claude_watch_data import _get_session_tasks
        tasks = _get_session_tasks(today_only=True)

        active_count = sum(1 for t in tasks if t.get("status") == "active")
        done_count = sum(1 for t in tasks if t.get("status") == "done")
        sessions = len(set(t.get("session_id", "") for t in tasks))

        self.query_one("#stasks-header", Static).update(
            f"[bold]Session Tasks — today[/bold]  "
            f"[green]{active_count} active[/green]  "
            f"[dim]{done_count} done  {sessions} sessions  "
            f"(Escape / q to go back)[/dim]"
        )

        dt = self.query_one("#stasks-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=5)
        dt.add_column("Status", width=8)
        dt.add_column("Session", width=10)
        dt.add_column("Project", width=12)
        dt.add_column("Task", width=40)
        dt.add_column("Started", width=10)
        dt.add_column("Duration", width=9)
        dt.add_column("Artifacts")

        for t in tasks:
            tid = str(t.get("id", ""))
            status = t.get("status", "?")
            status_icon = {"active": "●", "done": "✓", "blocked": "◼", "skipped": "○"}.get(status, "?")
            status_style = {"active": "green bold", "done": "dim", "blocked": "red", "skipped": "dim"}.get(status, "")

            session = t.get("session_id", "?")
            project = t.get("project", "—")
            task_name = (t.get("task_name") or "—")[:40]

            # Parse started_at time
            started = ""
            try:
                st = datetime.fromisoformat(t["started_at"].replace("Z", "+00:00"))
                started = st.astimezone().strftime("%H:%M")
            except Exception:
                pass

            # Compute duration
            duration = ""
            try:
                st = datetime.fromisoformat(t["started_at"].replace("Z", "+00:00"))
                if t.get("completed_at"):
                    et = datetime.fromisoformat(t["completed_at"].replace("Z", "+00:00"))
                else:
                    et = datetime.now(timezone.utc)
                mins = int((et - st).total_seconds() / 60)
                if mins >= 60:
                    duration = f"{mins // 60}h {mins % 60}m"
                else:
                    duration = f"{mins}m"
            except Exception:
                pass

            # Summarize artifacts
            artifacts = ""
            art = t.get("artifacts") or {}
            if isinstance(art, str):
                try:
                    art = json.loads(art)
                except Exception:
                    art = {}
            parts = []
            if art.get("files_edited"):
                parts.append(f"{len(art['files_edited'])} files")
            if art.get("commits"):
                parts.append(f"{len(art['commits'])} commits")
            if art.get("skills"):
                parts.append(f"{len(art['skills'])} skills")
            artifacts = ", ".join(parts) if parts else "—"

            dt.add_row(
                Text(tid, justify="right"),
                Text(f"{status_icon} {status}", style=status_style),
                Text(session, style="cyan"),
                Text(project),
                Text(task_name),
                Text(started, style="dim"),
                Text(duration),
                Text(artifacts, style="dim"),
            )

        if not tasks:
            dt.add_row("", Text("No session tasks today", style="dim"), "", "", "", "", "", "")

    def action_pop_screen(self):
        self.app.pop_screen()


class ProjectBoardScreen(Screen):
    """Project Monitor — strategic task board."""
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="pboard-header")
        with Horizontal(id="pboard-body"):
            yield Static(id="pboard-summary")
            yield DataTable(id="pboard-table")

    def on_mount(self):
        from claude_watch_data import _get_project_tasks
        tasks = _get_project_tasks()

        total = len(tasks)
        by_status = {}
        by_project = {}
        for t in tasks:
            s = t.get("status", "?")
            p = t.get("project", "?")
            by_status[s] = by_status.get(s, 0) + 1
            if p not in by_project:
                by_project[p] = {"ready": 0, "in_progress": 0, "built": 0, "blocked": 0}
            if s in by_project[p]:
                by_project[p][s] += 1

        ready = by_status.get("ready", 0)
        in_prog = by_status.get("in_progress", 0)
        built = by_status.get("built", 0)
        blocked = by_status.get("blocked", 0)

        self.query_one("#pboard-header", Static).update(
            f"[bold]Project Board[/bold]  "
            f"[yellow]{ready} ready[/yellow]  "
            f"[green]{in_prog} in progress[/green]  "
            f"[dim]{built} built  {blocked} blocked  {total} total  "
            f"(Escape / q to go back)[/dim]"
        )

        # Left panel: project summary
        summary_table = RichTable(show_header=True, show_edge=False, pad_edge=False)
        summary_table.add_column("Project", style="bold")
        summary_table.add_column("Co", style="dim")
        summary_table.add_column("Ready", style="yellow", justify="right")
        summary_table.add_column("Active", style="green", justify="right")
        summary_table.add_column("Built", style="dim", justify="right")
        summary_table.add_column("Blocked", style="red", justify="right")

        for proj in sorted(by_project.keys()):
            counts = by_project[proj]
            co_name, co_style = _project_to_company(proj)
            summary_table.add_row(
                proj,
                Text(co_name, style=co_style),
                str(counts["ready"]),
                str(counts["in_progress"]),
                str(counts["built"]),
                str(counts["blocked"]),
            )

        self.query_one("#pboard-summary", Static).update(
            Panel(summary_table, title="[bold]Summary[/bold]", border_style="cyan")
        )

        # Right panel: task list (in_progress first, then ready)
        dt = self.query_one("#pboard-table", DataTable)
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("#", width=5)
        dt.add_column("Status", width=12)
        dt.add_column("Project", width=12)
        dt.add_column("Co", width=8)
        dt.add_column("Task", width=35)
        dt.add_column("Phase", width=10)
        dt.add_column("Order", width=6)
        dt.add_column("Claimed", width=10)
        dt.add_column("Notes")

        # Sort: in_progress first, then ready, then blocked, then built
        status_order = {"in_progress": 0, "ready": 1, "blocked": 2, "built": 3, "archived": 4}
        sorted_tasks = sorted(tasks, key=lambda t: (
            status_order.get(t.get("status", ""), 9),
            t.get("build_order") or 9999,
        ))

        # Show in_progress + ready + blocked (skip built for readability, they can scroll)
        shown = [t for t in sorted_tasks if t.get("status") in ("in_progress", "ready", "blocked")]
        # Add up to 10 built at the end
        built_tasks = [t for t in sorted_tasks if t.get("status") == "built"][:10]
        shown.extend(built_tasks)

        for t in shown:
            tid = str(t.get("id", ""))
            status = t.get("status", "?")
            status_icon = {"in_progress": "●", "ready": "○", "blocked": "◼", "built": "✓", "archived": "—"}.get(status, "?")
            status_style = {"in_progress": "green bold", "ready": "yellow", "blocked": "red", "built": "dim"}.get(status, "")
            co_name, co_style = _project_to_company(t.get("project", ""))

            dt.add_row(
                Text(tid, justify="right"),
                Text(f"{status_icon} {status}", style=status_style),
                Text(t.get("project", "—"), style="cyan"),
                Text(co_name, style=co_style),
                Text((t.get("task_name") or "—")[:35]),
                Text((t.get("phase") or "—")[:10], style="dim"),
                Text(str(t.get("build_order") or "—"), justify="right"),
                Text((t.get("claimed_by") or "—")[:10], style="dim"),
                Text((t.get("notes") or "—")[:30], style="dim"),
            )

        if not shown:
            dt.add_row("", Text("No project tasks found", style="dim"), "", "", "", "", "", "", "")

        if built_tasks:
            remaining_built = len([t for t in sorted_tasks if t.get("status") == "built"]) - 10
            if remaining_built > 0:
                dt.add_row("", Text(f"... and {remaining_built} more built tasks", style="dim"), "", "", "", "", "", "", "")

    def action_pop_screen(self):
        self.app.pop_screen()


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

        if not sessions:
            self.add_row(
                "...", "", "", "", "", "", "", "", "", "",
                Text("building index..." if _index_building else "no sessions", style="dim"),
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


class NavBar(Horizontal):
    """Top navigation bar with clickable buttons."""

    def compose(self) -> ComposeResult:
        yield Button("Dashboard", id="nav-dashboard", variant="primary")
        yield Button("Sessions", id="nav-sessions", variant="default")
        yield Button("Projects", id="nav-projects", variant="default")
        yield Button("Usage", id="nav-usage", variant="default")
        yield Button("MCP", id="nav-mcp", variant="default")


# ── App ──────────────────────────────────────────────────────────────────────


class ClaudeWatchApp(App):
    CSS_PATH = "claude_watch_tui.tcss"
    TITLE = "claude-watch"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("u", "show_usage", "Usage"),
        Binding("m", "show_mcp", "MCP"),
        Binding("s", "show_session_tasks", "Tasks"),
        Binding("p", "show_project_board", "Board"),
        Binding("a", "toggle_accounts", "Accounts"),
        Binding("h", "toggle_health", "Health"),
        Binding("slash", "start_search", "Search"),
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
    ]

    _filter_text = ""

    def compose(self) -> ComposeResult:
        from textual.widgets import Input, Footer
        yield NavBar(id="nav-bar")
        with ScrollableContainer(id="main-scroll"):
            yield TokenHeader(id="header")
            yield AccountCapacityPanel(id="account-capacity")
            yield BurndownChart(id="burndown")
            yield Input(placeholder="Search sessions (ccid, project, directive)...", id="search-input")
            yield UrgentAlerts(id="urgent")
            yield SystemHealthPanel(id="system-health")
            yield ActiveSessionsTable(id="active-sessions")
            yield SessionHistoryTable(id="session-history")
            yield DrainPanel(id="drain")
            with Horizontal(id="feed-row"):
                yield ToolFrequency(id="tool-freq")
                yield SkillsPanel(id="skills")
                yield AgentsPanel(id="agents")
        yield Footer()

    def on_mount(self):
        _load_index()
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
        """Restart the process when source files change."""
        self.notify("Reloading...", severity="warning", timeout=1)
        self.set_timer(0.5, lambda: self.exit(return_code=self._RESTART_EXIT_CODE))

    def build_index(self):
        import threading
        t = threading.Thread(target=_build_or_update_index, daemon=True)
        t.start()

    def refresh_data(self):
        five, seven, fr, sr = _current_pct()
        self.query_one("#header", TokenHeader).update_content(five, seven, fr, sr)
        acct = self.query_one("#account-capacity", AccountCapacityPanel)
        if acct.display:
            acct.update_content()
        self.query_one("#burndown", BurndownChart).update_content()
        self.query_one("#urgent", UrgentAlerts).update_content()
        self.query_one("#active-sessions", ActiveSessionsTable).refresh_rows()
        health = self.query_one("#system-health", SystemHealthPanel)
        if health.display:
            health.update_content()
        self.query_one("#session-history", SessionHistoryTable).refresh_rows()
        self.query_one("#tool-freq", ToolFrequency).update_content()
        self.query_one("#skills", SkillsPanel).update_content()
        self.query_one("#agents", AgentsPanel).update_content()
        # CallHistoryTable removed — merged into SessionHistoryTable sub-rows
        self.query_one("#drain", DrainPanel).update_content()

    def action_force_refresh(self):
        self.build_index()
        self.refresh_data()

    def action_show_usage(self):
        self.push_screen(UsageMetricsScreen())

    def action_show_mcp(self):
        self.push_screen(MCPStatsScreen())

    def action_show_session_tasks(self):
        self.push_screen(SessionTasksScreen())

    def action_show_project_board(self):
        self.push_screen(ProjectBoardScreen())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "nav-dashboard":
            # Already on dashboard — pop any overlay screens
            while len(self.screen_stack) > 1:
                self.pop_screen()
        elif btn_id == "nav-sessions":
            self.action_show_session_tasks()
        elif btn_id == "nav-projects":
            self.action_show_project_board()
        elif btn_id == "nav-usage":
            self.action_show_usage()
        elif btn_id == "nav-mcp":
            self.action_show_mcp()

    def action_toggle_accounts(self):
        acct = self.query_one("#account-capacity", AccountCapacityPanel)
        if acct.display:
            acct.display = False
        else:
            acct.display = True
            acct.update_content()

    def action_toggle_health(self):
        health = self.query_one("#system-health", SystemHealthPanel)
        if health.display:
            health.display = False
        else:
            health.display = True
            health.update_content()

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
