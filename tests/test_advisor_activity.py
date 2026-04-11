"""Tests for ``advisor_activity`` — wire-traffic roll-up widget.

Verifies the aggregation logic and rendering without touching Textual
or the network. ``fetch_messages`` uses a dependency-injected fetcher
specifically so we can exercise it with canned responses here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from advisor_activity import (
    WorkerActivity,
    aggregate_activity,
    fetch_messages,
    render_advisor_activity,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _msg(
    from_sid: str,
    to_sid: str,
    msg_type: str,
    payload: Dict[str, Any],
    minutes_ago: float,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "from_session": from_sid,
        "to_session": to_sid,
        "msg_type": msg_type,
        "payload": payload,
        "created_at": _iso(now - timedelta(minutes=minutes_ago)),
    }


# ---------------------------------------------------------------------------
# aggregate_activity
# ---------------------------------------------------------------------------


class TestAggregateActivity:
    def test_empty_messages(self):
        assert aggregate_activity([], "cc-advisor") == []

    def test_single_task_complete_registers_worker(self):
        msgs = [
            _msg(
                "cc-1111",
                "cc-advisor",
                "status",
                {
                    "status": "task_complete",
                    "task_id": "131369",
                    "task_name": "Dispatch grid",
                    "project": "token-watch",
                },
                3,
            ),
        ]
        out = aggregate_activity(msgs, "cc-advisor")
        assert len(out) == 1
        wa = out[0]
        assert wa.worker == "cc-1111"
        assert wa.last_status == "task_complete"
        assert wa.last_task_id == "131369"
        assert wa.completed == 1
        assert "token-watch" in wa.projects

    def test_handoff_counts_on_recipient(self):
        msgs = [
            _msg(
                "cc-advisor",
                "cc-2222",
                "task_handoff",
                {"task_id": "99", "task_name": "do the thing"},
                2,
            ),
        ]
        out = aggregate_activity(msgs, "cc-advisor")
        assert len(out) == 1
        wa = out[0]
        assert wa.worker == "cc-2222"
        assert wa.handoffs_in == 1
        assert wa.last_status == "task_handoff"

    def test_advisor_self_excluded(self):
        """Messages where the advisor is the worker side must not produce a row."""
        msgs = [
            _msg("cc-advisor", "cc-advisor", "status", {"status": "heartbeat"}, 1),
        ]
        assert aggregate_activity(msgs, "cc-advisor") == []

    def test_blocked_counter(self):
        msgs = [
            _msg(
                "cc-3333",
                "cc-advisor",
                "question",
                {
                    "blocker_type": "file_lock",
                    "message": "blocked: file_lock on tui.py",
                },
                5,
            ),
        ]
        out = aggregate_activity(msgs, "cc-advisor")
        assert out[0].blocked == 1

    def test_multiple_workers_sorted_by_recency(self):
        msgs = [
            _msg("cc-old", "cc-advisor", "status", {"status": "task_complete"}, 10),
            _msg("cc-new", "cc-advisor", "status", {"status": "task_complete"}, 1),
            _msg("cc-mid", "cc-advisor", "status", {"status": "task_complete"}, 5),
        ]
        out = aggregate_activity(msgs, "cc-advisor")
        assert [w.worker for w in out] == ["cc-new", "cc-mid", "cc-old"]

    def test_payload_as_json_string(self):
        """Some rows store payload as a JSON string — must be parsed."""
        import json as _json

        msgs = [
            {
                "from_session": "cc-4444",
                "to_session": "cc-advisor",
                "msg_type": "status",
                "payload": _json.dumps({"status": "task_complete", "task_id": "77"}),
                "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=2)),
            }
        ]
        out = aggregate_activity(msgs, "cc-advisor")
        assert out[0].completed == 1
        assert out[0].last_task_id == "77"

    def test_counters_accumulate(self):
        msgs = [
            _msg("cc-5555", "cc-advisor", "status", {"status": "task_complete"}, 9),
            _msg("cc-5555", "cc-advisor", "status", {"status": "task_complete"}, 6),
            _msg("cc-advisor", "cc-5555", "task_handoff", {"task_id": "1"}, 5),
            _msg("cc-5555", "cc-advisor", "question", {"message": "blocked: lock"}, 4),
        ]
        out = aggregate_activity(msgs, "cc-advisor")
        assert len(out) == 1
        wa = out[0]
        assert wa.completed == 2
        assert wa.handoffs_in == 1
        assert wa.blocked == 1


# ---------------------------------------------------------------------------
# fetch_messages
# ---------------------------------------------------------------------------


class TestFetchMessages:
    def test_empty_advisor_returns_empty(self):
        calls: List[str] = []

        def fetcher(url: str):
            calls.append(url)
            return []

        assert fetch_messages(fetcher, "") == []
        assert calls == [], "no fetch should happen when advisor_sid is blank"

    def test_fetcher_called_with_filter_clause(self):
        captured: List[str] = []

        def fetcher(url: str):
            captured.append(url)
            return []

        fetch_messages(fetcher, "cc-advisor", lookback_minutes=15, limit=50)
        assert captured, "fetcher should be invoked"
        url = captured[0]
        assert "cc-advisor" in url
        assert "msg_type=in.(status,task_handoff,question)" in url
        assert "limit=50" in url
        assert "created_at=gt." in url

    def test_fetcher_exception_returns_empty(self):
        def broken(_url: str):
            raise RuntimeError("network flake")

        assert fetch_messages(broken, "cc-advisor") == []

    def test_non_list_response_returns_empty(self):
        def weird(_url: str):
            return {"not": "a list"}

        assert fetch_messages(weird, "cc-advisor") == []


# ---------------------------------------------------------------------------
# render_advisor_activity
# ---------------------------------------------------------------------------


class TestRender:
    def test_empty_renders_placeholder(self):
        out = render_advisor_activity("cc-advisor", [])
        # Render to plain text to inspect
        from rich.console import Console
        from io import StringIO

        buf = StringIO()
        Console(file=buf, width=120, color_system=None, force_terminal=False).print(out)
        text = buf.getvalue()
        assert "Advisor Activity" in text
        assert "no wire traffic" in text

    def test_header_and_row_present(self):
        wa = WorkerActivity(worker="cc-1111")
        wa.last_status = "task_complete"
        wa.last_ts = datetime.now(timezone.utc) - timedelta(minutes=3)
        wa.last_task_id = "131369"
        wa.last_task_name = "Dispatch grid"
        wa.completed = 1
        wa.projects.add("token-watch")

        out = render_advisor_activity("cc-advisor", [wa])
        from rich.console import Console
        from io import StringIO

        buf = StringIO()
        Console(file=buf, width=140, color_system=None, force_terminal=False).print(out)
        text = buf.getvalue()
        assert "Advisor Activity" in text
        assert "cc-advisor" in text
        assert "cc1111" in text
        assert "task_complete" in text
        assert "Dispatch grid" in text
        assert "token-watch" in text
