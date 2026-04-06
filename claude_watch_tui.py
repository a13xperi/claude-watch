#!/usr/bin/env python3
"""
claude-watch TUI — Textual-based interactive dashboard for Claude Code token monitoring.
Scrollable panels, keyboard navigation, no dead space.
"""

from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Static

from claude_watch_data import (
    make_active_calls_panel,
    make_urgent_panel,
    _abbrev_model,
    _build_or_update_index,
    _compute_tool_feed_rows,
    _current_pct,
    _get_session_history,
    _index_building,
    _load_index,
    make_drain_panel,
    make_header,
    make_sessions_panel,
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


class DrainPanel(Static):
    def update_content(self):
        self.update(make_drain_panel())


# ── DataTable widgets (scrollable) ───────────────────────────────────────────


class SessionHistoryTable(DataTable):
    BORDER_TITLE = "Session History"
    BORDER_SUBTITLE = "Tab to focus · arrows to scroll"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        # Merged Time+Dur, matching Rich version layout
        self.add_column("Time    Dur", width=13)
        self.add_column("Session", width=9)
        self.add_column("Src", width=10)
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
                "...", "", "", "", "", "",
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
                self.add_row(Text(sep, style="dim"), "", "", "", "", "", "", key=f"sep-{group}")
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
            src_style = "yellow" if src == "paperclip" else ("green" if src == "cli" else ("cyan" if "atlas" in src else "dim"))
            short_id = s["session_id"][:8]
            self.add_row(
                Text(time_dur, style="dim"),
                Text(short_id, style="dim"),
                Text(src, style=src_style),
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


class ToolCallFeed(DataTable):
    BORDER_TITLE = "Tool Call Feed (newest first)"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_column("Time", width=9)
        self.add_column("Session", width=9)
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
            self.add_row("...", "", "", "", "", "", Text("no events yet", style="dim"))
            return

        for r in rows:
            # Derive source from directive heuristic
            d = r["directive"].lower()
            if "morning" in d or "brief" in d or "monitor" in d or "health" in d:
                src, sc = "paperclip", "yellow"
            elif "unnamed" in d:
                src, sc = "?", "dim"
            else:
                src, sc = "cli", "green"
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


# ── App ──────────────────────────────────────────────────────────────────────


class ClaudeWatchApp(App):
    CSS_PATH = "claude_watch_tui.tcss"
    TITLE = "claude-watch"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield TokenHeader(id="header")
        yield UrgentAlerts(id="urgent")
        yield ActiveSessions(id="active-sessions")
        yield ActiveCalls(id="active-calls")
        yield SessionHistoryTable(id="session-history")
        with Horizontal(id="feed-row"):
            yield ToolCallFeed(id="tool-feed")
            yield ToolFrequency(id="tool-freq")
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
        self.query_one("#drain", DrainPanel).update_content()

    def action_force_refresh(self):
        self.build_index()
        self.refresh_data()


def main():
    app = ClaudeWatchApp()
    app.run()


if __name__ == "__main__":
    main()
