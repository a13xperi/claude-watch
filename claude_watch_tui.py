#!/usr/bin/env python3
"""
claude-watch TUI — Textual-based interactive dashboard for Claude Code token monitoring.
Scrollable panels, keyboard navigation, no dead space.
"""

from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Header, Static

from claude_watch_data import (
    make_active_calls_panel,
    make_urgent_panel,
    _abbrev_model,
    _build_or_update_index,
    _compute_tool_feed_rows,
    _current_pct,
    _get_call_history,
    _get_session_history,
    _get_session_turns,
    _get_usage_metrics,
    _index_building,
    _load_index,
    make_drain_panel,
    make_header,
    make_sessions_panel,
    make_skills_panel,
    make_tool_stats,
)

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


class ActiveSessions(Static):
    def update_content(self):
        self.update(make_sessions_panel())


class ToolFrequency(Static):
    def update_content(self):
        self.update(make_tool_stats())


class ActiveCalls(Static):
    def update_content(self):
        self.update(make_active_calls_panel())


class SkillsPanel(Static):
    def update_content(self):
        self.update(make_skills_panel())


class DrainPanel(Static):
    def update_content(self):
        self.update(make_drain_panel())


# ── Drill-down screen ────────────────────────────────────────────────────────


class SessionDrillDown(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, session_id, directive=""):
        super().__init__()
        self.session_id = session_id
        self.session_directive = directive

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold]Session:[/bold] {self.session_id}  "
            f"[bold]Directive:[/bold] {self.session_directive}  "
            "[dim](Escape to go back)[/dim]",
            id="drilldown-header",
        )
        yield DataTable(id="drilldown-table")

    def on_mount(self):
        table = self.query_one("#drilldown-table", DataTable)
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

        # Summary row
        table.add_row(
            Text("Σ", style="bold"),
            Text(f"{total_in/1000:.0f}k", style="bold"),
            Text(f"{total_out/1000:.0f}k", style="bold"),
            Text(f"{total_pct:.1f}%", style="bold yellow"),
            "",
            "",
            Text(f"{len(turns)} turns", style="bold"),
        )

    def action_pop_screen(self):
        self.app.pop_screen()


class UsageMetricsScreen(Screen):
    BINDINGS = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="metrics-header")
        yield DataTable(id="metrics-table")
        yield Static(id="metrics-summary")

    def on_mount(self):
        metrics, total = _get_usage_metrics(days=7)
        _, seven, _, _ = _current_pct()

        self.query_one("#metrics-header", Static).update(
            f"[bold]Usage Metrics — last 7 days[/bold]  "
            f"[dim]Total output: {total/1000:.0f}k tokens  "
            f"Account 7d: {seven}%  (Escape to go back)[/dim]"
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


# ── DataTable widgets (scrollable) ───────────────────────────────────────────


class SessionHistoryTable(DataTable):
    BORDER_TITLE = "Session History"
    BORDER_SUBTITLE = "Tab to focus · Enter to drill down · arrows to scroll"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        # Merged Time+Dur, matching Rich version layout
        self.add_column("Time    Dur", width=13)
        self.add_column("Who", width=18)
        self.add_column("Mdl", width=7)
        self.add_column("~5h%", width=7)
        self.add_column("Out", width=6)
        self.add_column("Directive")

    def refresh_rows(self):
        sessions = _get_session_history()

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        if not sessions:
            self.add_row(
                "...", "", "", "", "",
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
                sep = f"── {group} " + "─" * 30
                self.add_row(Text(sep, style="dim"), "", "", "", "", "", key=f"sep-{group}")
                current_group = group

            end_str = s["last_ts"].astimezone().strftime("%H:%M")
            time_dur = f"{end_str} {s['dur_str']:>6}"

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

            src = s.get("source", "?")
            src_style = "yellow" if ("/" in src or src == "paperclip") else ("green" if src == "cli" else ("cyan" if "atlas" in src else "dim"))
            who = f"{src}/{(s['directive'] or '?')[:8]}"
            self.add_row(
                Text(time_dur, style="dim"),
                Text(who, style=src_style),
                Text(mdl, style=mdl_style),
                Text(pct, style=pct_style),
                Text(out_str, style="dim", justify="right"),
                directive,
                key=s["session_id"],
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row)
        except Exception:
            pass

    def on_data_table_row_selected(self, event):
        key = event.row_key
        if key and key.value and not key.value.startswith("sep-"):
            session_id = key.value
            # Find directive from index
            sessions = _get_session_history()
            directive = "—"
            for s in sessions:
                if s["session_id"] == session_id:
                    directive = s.get("directive", "—")
                    break
            self.app.push_screen(SessionDrillDown(session_id, directive))


class ToolCallFeed(DataTable):
    BORDER_TITLE = "Tool Call Feed (newest first)"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_column("Time", width=9)
        self.add_column("Who", width=14)
        self.add_column("Δ5h%", width=5)
        self.add_column("Tool", width=8)
        self.add_column("Src", width=10)
        self.add_column("Directive")

    def refresh_rows(self):
        rows = _compute_tool_feed_rows(last_n=200)

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        if not rows:
            self.add_row("...", "", "", "", "", Text("no events yet", style="dim"))
            return

        for r in rows:
            src = r.get("source", "cli")
            sc = "yellow" if ("/" in src or src == "paperclip") else ("green" if src == "cli" else "dim")
            self.add_row(
                Text(r["ts_str"], style="dim"),
                Text(r["session"], style="cyan"),
                Text(r["delta_str"], style=r["delta_style"]),
                r["tool"],
                Text(src, style=sc),
                Text(r["directive"][:30], style="dim"),
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row)
        except Exception:
            pass


class CallHistoryTable(DataTable):
    BORDER_TITLE = "Call History (all sessions from ledger)"
    BORDER_SUBTITLE = "Tab to focus"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_column("When", width=6)
        self.add_column("Session", width=12)
        self.add_column("Src", width=10)
        self.add_column("Calls", width=5)
        self.add_column("Tools Used", width=28)
        self.add_column("5h%", width=7)
        self.add_column("Directive")

    def refresh_rows(self):
        history = _get_call_history()

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        if not history:
            self.add_row("...", "", "", "", "", "", Text("no data", style="dim"))
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
                sep = f"── {group} " + "─" * 30
                self.add_row(Text(sep, style="dim"), "", "", "", "", "", "", key=f"ch-sep-{group}")
                current_group = group

            src = h["source"]
            src_style = "yellow" if ("/" in src or src == "paperclip") else ("green" if src == "cli" else "dim")

            pct = h["pct_str"]
            try:
                v = float(pct.strip("+%"))
                pct_style = "red" if v > 5 else ("yellow" if v > 2 else "green")
            except Exception:
                pct_style = "dim"

            self.add_row(
                Text(h["when"], style="dim"),
                Text(h["session"][:12], style="cyan"),
                Text(src, style=src_style),
                Text(str(h["calls"]), justify="right"),
                Text(h["tools_str"][:28], style="dim"),
                Text(pct, style=pct_style),
                Text((h["directive"] or "—")[:40]),
                key=f"ch-{h['session']}",
            )

        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row)
        except Exception:
            pass


# ── App ──────────────────────────────────────────────────────────────────────


class ClaudeWatchApp(App):
    CSS_PATH = "claude_watch_tui.tcss"
    TITLE = "claude-watch"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("u", "show_usage", "Usage"),
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield TokenHeader(id="header")
        yield UrgentAlerts(id="urgent")
        yield ActiveSessions(id="active-sessions")
        yield ActiveCalls(id="active-calls")
        with Horizontal(id="history-row"):
            yield CallHistoryTable(id="call-history")
        with Horizontal(id="feed-row"):
            yield ToolCallFeed(id="tool-feed")
            with Vertical(id="stats-col"):
                yield ToolFrequency(id="tool-freq")
                yield SkillsPanel(id="skills")
        yield SessionHistoryTable(id="session-history")
        yield DrainPanel(id="drain")

    def on_mount(self):
        _load_index()
        self.build_index()
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()

    def build_index(self):
        import threading
        t = threading.Thread(target=_build_or_update_index, daemon=True)
        t.start()

    def refresh_data(self):
        five, seven, fr, sr = _current_pct()
        self.query_one("#header", TokenHeader).update_content(five, seven, fr, sr)
        self.query_one("#urgent", UrgentAlerts).update_content()
        self.query_one("#active-sessions", ActiveSessions).update_content()
        self.query_one("#active-calls", ActiveCalls).update_content()
        self.query_one("#session-history", SessionHistoryTable).refresh_rows()
        self.query_one("#tool-feed", ToolCallFeed).refresh_rows()
        self.query_one("#tool-freq", ToolFrequency).update_content()
        self.query_one("#skills", SkillsPanel).update_content()
        self.query_one("#call-history", CallHistoryTable).refresh_rows()
        self.query_one("#drain", DrainPanel).update_content()

    def action_force_refresh(self):
        self.build_index()
        self.refresh_data()

    def action_show_usage(self):
        self.push_screen(UsageMetricsScreen())


def main():
    app = ClaudeWatchApp()
    app.run()


if __name__ == "__main__":
    main()
