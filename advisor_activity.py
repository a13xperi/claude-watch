"""AdvisorActivity — live wire-traffic panel for the advisor session.

Shows at a glance what the advisor has been doing in the last 15 minutes:
which workers just completed tasks, which have pending task_handoffs,
and which have fired blockers. Mirrors the ``dispatch_grid.py`` pattern:

* standalone module, zero ``token_watch_tui.py`` edits required
* lazy import of ``token_watch_data`` inside ``update_content``
* Rich-only rendering so it's trivially unit-testable without Textual

The advisor's reaction loop (``/advisor-react``) polls ``session_messages``
on a 60s cron. This widget gives a HUMAN a live view of that same traffic
so Alex can tell whether the advisor is keeping up, which workers are
parked on blockers, and when the wire last saw a ``task_complete``.

Data source:
    Supabase ``session_messages`` table, filtered to the last 15 minutes
    of status + task_handoff + question messages involving the advisor.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from rich.console import Group
from rich.markup import escape as _rich_escape
from rich.panel import Panel
from rich.table import Table as RichTable
from rich.text import Text
from textual.widgets import Static


_LOOKBACK_MINUTES = 15
_MESSAGE_LIMIT = 200


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class WorkerActivity:
    worker: str
    last_status: str = "idle"
    last_ts: Optional[datetime] = None
    last_task_id: str = ""
    last_task_name: str = ""
    completed: int = 0
    blocked: int = 0
    handoffs_in: int = 0
    projects: set = field(default_factory=set)

    @property
    def minutes_since(self) -> Optional[float]:
        if not self.last_ts:
            return None
        delta = datetime.now(timezone.utc) - self.last_ts
        return delta.total_seconds() / 60.0


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _normalise_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def fetch_messages(
    fetcher,
    advisor_sid: str,
    lookback_minutes: int = _LOOKBACK_MINUTES,
    limit: int = _MESSAGE_LIMIT,
) -> List[Dict[str, Any]]:
    """Return the last ``lookback_minutes`` of messages involving ``advisor_sid``.

    ``fetcher`` is a callable ``(url) -> list[dict]``. Extracted for tests
    so the widget can be exercised without touching the network.
    """
    if not advisor_sid:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    url = (
        f"session_messages?created_at=gt.{cutoff}"
        f"&or=(to_session.eq.{advisor_sid},from_session.eq.{advisor_sid})"
        f"&msg_type=in.(status,task_handoff,question)"
        f"&order=created_at.asc&limit={limit}"
    )
    try:
        rows = fetcher(url)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return rows


def aggregate_activity(
    messages: List[Dict[str, Any]],
    advisor_sid: str,
) -> List[WorkerActivity]:
    """Roll up wire messages into per-worker activity records.

    Worker identity is ``from_session`` for outbound status/questions and
    ``to_session`` for inbound task_handoffs. The advisor itself is
    excluded from the output so the panel only shows worker rows.
    """
    workers: Dict[str, WorkerActivity] = {}

    for m in messages:
        msg_type = m.get("msg_type") or ""
        from_sid = m.get("from_session") or ""
        to_sid = m.get("to_session") or ""
        payload = _normalise_payload(m.get("payload"))
        ts = _parse_ts(m.get("created_at"))

        if msg_type in ("status", "question"):
            worker = from_sid
        elif msg_type == "task_handoff":
            worker = to_sid
        else:
            continue

        if not worker or worker == advisor_sid:
            continue

        wa = workers.setdefault(worker, WorkerActivity(worker=worker))

        # Task metadata extracted from payload
        status_key = payload.get("status") or msg_type
        task_id = str(payload.get("task_id") or "")
        task_name = str(payload.get("task_name") or "")
        project = payload.get("project")
        if project:
            wa.projects.add(str(project))

        # Advance "last" fields if this message is newer
        if ts and (wa.last_ts is None or ts > wa.last_ts):
            wa.last_ts = ts
            wa.last_status = status_key
            if task_id:
                wa.last_task_id = task_id
            if task_name:
                wa.last_task_name = task_name

        # Counters
        if status_key == "task_complete":
            wa.completed += 1
        if msg_type == "question" or "blocked" in str(payload.get("message", "")).lower():
            wa.blocked += 1
        if msg_type == "task_handoff":
            wa.handoffs_in += 1

    def sort_key(wa: WorkerActivity):
        recency = wa.last_ts.timestamp() if wa.last_ts else 0.0
        return (-recency, wa.worker)

    return sorted(workers.values(), key=sort_key)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_STATUS_COLORS = {
    "task_complete": "green",
    "task_handoff": "cyan",
    "lane_drained": "yellow",
    "discovery": "magenta",
    "question": "red",
    "blocked": "red",
    "exec_error": "red",
    "idle": "grey50",
}


def _status_style(label: str) -> str:
    return _STATUS_COLORS.get(label, "grey66")


def _format_age(minutes: Optional[float]) -> str:
    if minutes is None:
        return "—"
    if minutes < 1:
        return f"{int(minutes * 60)}s"
    if minutes < 60:
        return f"{int(minutes)}m"
    return f"{int(minutes / 60)}h"


def render_advisor_activity(
    advisor_sid: str,
    workers: List[WorkerActivity],
) -> Group:
    header = Text()
    header.append("Advisor Activity  ", style="bold")
    if advisor_sid:
        header.append(f"{advisor_sid}  ", style="cyan")
    header.append(f"last {_LOOKBACK_MINUTES}m", style="dim italic")

    if not workers:
        empty = Text(
            "  (no wire traffic — advisor may be dead or everyone's idle)",
            style="dim italic",
        )
        return Group(header, Text(""), empty)

    table = RichTable(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        expand=False,
    )
    table.add_column("Worker", width=10, no_wrap=True)
    table.add_column("Last", width=14, no_wrap=True)
    table.add_column("Age", justify="right", width=5)
    table.add_column("Done", justify="right", width=5)
    table.add_column("Blocked", justify="right", width=8)
    table.add_column("Handoffs", justify="right", width=9)
    table.add_column("Last Task", width=30, no_wrap=True)
    table.add_column("Projects", width=18, no_wrap=True)

    for wa in workers:
        label = wa.last_status
        color = _status_style(label)
        projects = ",".join(sorted(wa.projects)) if wa.projects else "—"
        task_display = wa.last_task_name or wa.last_task_id or "—"
        if len(task_display) > 28:
            task_display = task_display[:27] + "…"
        table.add_row(
            Text(_rich_escape(wa.worker.replace("cc-", "cc")), style="bold"),
            Text(_rich_escape(label), style=color),
            _format_age(wa.minutes_since),
            Text(str(wa.completed), style="green" if wa.completed else "dim"),
            Text(str(wa.blocked), style="red" if wa.blocked else "dim"),
            Text(str(wa.handoffs_in), style="cyan" if wa.handoffs_in else "dim"),
            Text(_rich_escape(task_display)),
            Text(_rich_escape(projects), style="dim"),
        )

    return Group(header, Text(""), table)


# ---------------------------------------------------------------------------
# Textual widget
# ---------------------------------------------------------------------------


class AdvisorActivity(Static):
    """Textual widget showing recent wire traffic with the advisor.

    Mount with ``yield AdvisorActivity(id="advisor-activity")`` inside a
    parent view's ``compose``. Callers should invoke ``update_content``
    on load/refresh. Internally it resolves the advisor via
    ``session_locks?role=advisor`` so it survives advisor-session swaps
    (e.g. cc-18721 dying and being replaced by cc-46255).
    """

    DEFAULT_CSS = """
    AdvisorActivity {
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    def on_mount(self) -> None:  # pragma: no cover — Textual lifecycle
        self.update_content()

    def update_content(self) -> None:
        try:
            advisor_sid = self._resolve_advisor()
            messages = self._load_messages(advisor_sid)
            workers = aggregate_activity(messages, advisor_sid)
            self.update(render_advisor_activity(advisor_sid, workers))
        except Exception as exc:
            self.update(Text(f"[AdvisorActivity] error: {exc}", style="red"))

    # -- data layer ---------------------------------------------------------

    def _resolve_advisor(self) -> str:
        from token_watch_data import _SUPABASE_URL, __SUPABASE_KEY
        url = f"{_SUPABASE_URL}/session_locks?status=eq.active&role=eq.advisor&select=session_id&limit=1"
        rows = _supabase_get(url, __SUPABASE_KEY)
        if rows:
            return rows[0].get("session_id", "") or ""
        return ""

    def _load_messages(self, advisor_sid: str) -> List[Dict[str, Any]]:
        from token_watch_data import _SUPABASE_URL, __SUPABASE_KEY

        def fetcher(rel_url: str) -> List[Dict[str, Any]]:
            url = f"{_SUPABASE_URL}/{rel_url}"
            return _supabase_get(url, __SUPABASE_KEY) or []

        return fetch_messages(fetcher, advisor_sid)


def _supabase_get(url: str, key: str) -> Optional[List[Dict[str, Any]]]:
    """Minimal GET helper. Returns None on any error (widget handles it)."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"), strict=False)
            if isinstance(data, list):
                return data
            return None
    except Exception:
        return None


__all__ = [
    "AdvisorActivity",
    "WorkerActivity",
    "aggregate_activity",
    "fetch_messages",
    "render_advisor_activity",
]
