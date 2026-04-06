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
from textual.widgets import DataTable, Static

from claude_watch_data import (
    _abbrev_model,
    _build_or_update_index,
    _compute_tool_feed_rows,
    _current_pct,
    _ensure_index,
    _get_session_history,
    _index_building,
    _load_index,
    make_drain_panel,
    make_header,
    make_sessions_panel,
    make_tool_stats,
)

# ── Static widgets (wrap existing Rich renderables) ──────────────────────────


class TokenHeader(Static):
    def update_content(self, five, seven, fr, sr):
        self.update(make_header(five, seven, fr, sr))


class ActiveSessions(Static):
    def update_content(self):
        self.update(make_sessions_panel())


class ToolFrequency(Static):
    def update_content(self):
        self.update(make_tool_stats())


class DrainPanel(Static):
    def update_content(self):
        self.update(make_drain_panel())


# ── DataTable widgets (scrollable) ───────────────────────────────────────────


class SessionHistoryTable(DataTable):
    BORDER_TITLE = "Session History"
    BORDER_SUBTITLE = "Tab to focus, arrows to scroll"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("Time", "Dur", "Model", "~5h%", "OutTok", "Directive / last prompt")

    def refresh_rows(self):
        sessions = _get_session_history()

        # Preserve scroll position
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

            directive = (s["directive"] or "—")[:50]

            self.add_row(
                Text(end_str, style="dim"),
                Text(s["dur_str"], style="dim"),
                Text(mdl, style=mdl_style),
                Text(pct, style=pct_style),
                Text(out_str, style="dim"),
                directive,
                key=s["session_id"],
            )

        # Restore scroll position
        try:
            if cur_row < self.row_count:
                self.move_cursor(row=cur_row)
        except Exception:
            pass


class ToolCallFeed(DataTable):
    BORDER_TITLE = "Tool Call Feed"
    BORDER_SUBTITLE = "newest first"

    def on_mount(self):
        self.cursor_type = "row"
        self.zebra_stripes = True
        # Fixed column order: Time, Session, Tool, Directive, Δ5h%
        self.add_columns("Time", "Session", "Tool", "Directive", "Δ5h%")

    def refresh_rows(self):
        rows = _compute_tool_feed_rows(last_n=200)

        try:
            cur_row = self.cursor_row
        except Exception:
            cur_row = 0

        self.clear()

        if not rows:
            self.add_row("...", "", "", Text("no events yet", style="dim"), "")
            return

        for r in rows:
            self.add_row(
                Text(r["ts_str"], style="dim"),
                Text(r["session"], style="cyan"),
                r["tool"],
                Text(r["directive"][:30], style="dim"),
                Text(r["delta_str"], style=r["delta_style"]),
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
        yield ActiveSessions(id="active-sessions")
        yield SessionHistoryTable(id="session-history")
        with Horizontal(id="feed-row"):
            yield ToolCallFeed(id="tool-feed")
            yield ToolFrequency(id="tool-freq")
        yield DrainPanel(id="drain")

    def on_mount(self):
        _load_index()
        self.build_index()
        self.set_interval(1.0, self.refresh_data)
        # Initial populate
        self.refresh_data()

    def build_index(self):
        import threading
        t = threading.Thread(target=_build_or_update_index, daemon=True)
        t.start()

    def refresh_data(self):
        five, seven, fr, sr = _current_pct()
        self.query_one("#header", TokenHeader).update_content(five, seven, fr, sr)
        self.query_one("#active-sessions", ActiveSessions).update_content()
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
