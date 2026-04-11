"""Regression tests for ``_get_session_activity_history``.

Feeds the "click a session in Dispatch tab → show recent claims"
drill-down panel. ``project_tasks.claimed_by`` clears on ``/done`` so
the obvious source returns empty for recent completions; the truthful
source is ``build_ledger`` which records each feature/fix/decision
with session_id and commit_sha.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import token_watch_data as twd


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class _FakeResp:
    def __init__(self, rows):
        self._body = json.dumps(rows).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_returning(rows):
    def fake_urlopen(req, timeout=5):
        return _FakeResp(rows)
    return fake_urlopen


def _clear_cache():
    twd._ACTIVITY_HISTORY_CACHE.clear()


# ---------------------------------------------------------------------------


class TestBasicShape:
    def setup_method(self):
        _clear_cache()

    def test_empty_session_id_returns_empty(self):
        assert twd._get_session_activity_history("") == []

    def test_empty_response_returns_empty(self):
        with patch("urllib.request.urlopen", _urlopen_returning([])):
            assert twd._get_session_activity_history("cc-9930") == []

    def test_network_error_returns_empty(self):
        def broken(req, timeout=5):
            raise RuntimeError("down")

        with patch("urllib.request.urlopen", broken):
            assert twd._get_session_activity_history("cc-9930") == []

    def test_single_entry_enriched(self):
        now = datetime.now(timezone.utc)
        rows = [
            {
                "id": "uuid-1",
                "session_id": "cc-9930",
                "item_type": "feature",
                "title": "Burn-rate sparkline",
                "project": "token-watch",
                "company": "personal",
                "commit_sha": "6c5faeb",
                "test_status": "tested",
                "files": ["token_watch_data.py", "token_watch_tui.py"],
                "created_at": _iso(now - timedelta(minutes=5)),
            }
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            result = twd._get_session_activity_history("cc-9930", limit=5)
        assert len(result) == 1
        entry = result[0]
        assert entry["id"] == "uuid-1"
        assert entry["title"] == "Burn-rate sparkline"
        assert entry["commit_sha"] == "6c5faeb"
        assert entry["item_type"] == "feature"
        assert entry["status_color"] == "green"
        assert entry["age_minutes"] is not None and 4 <= entry["age_minutes"] <= 6
        assert entry["files"] == ["token_watch_data.py", "token_watch_tui.py"]


class TestItemTypeColors:
    def setup_method(self):
        _clear_cache()

    @pytest.mark.parametrize(
        "item_type,expected_color",
        [
            ("feature", "green"),
            ("fix", "yellow"),
            ("test", "cyan"),
            ("decision", "magenta"),
            ("idea", "blue"),
            ("refactor", "grey66"),
            ("unknown_type", "grey66"),
            ("", "grey66"),
        ],
    )
    def test_status_color_mapping(self, item_type, expected_color):
        now = datetime.now(timezone.utc)
        rows = [
            {
                "id": "x",
                "session_id": "cc-1",
                "item_type": item_type,
                "title": "t",
                "project": "p",
                "company": "c",
                "commit_sha": "sha",
                "test_status": "tested",
                "files": [],
                "created_at": _iso(now - timedelta(minutes=1)),
            }
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            result = twd._get_session_activity_history("cc-1")
        assert result[0]["status_color"] == expected_color


class TestLimitClamping:
    def setup_method(self):
        _clear_cache()

    def test_negative_limit_clamps_to_one(self):
        """limit < 1 should be clamped — verified via the URL captured."""
        captured = {}

        def capturing_urlopen(req, timeout=5):
            captured["url"] = req.full_url
            return _FakeResp([])

        with patch("urllib.request.urlopen", capturing_urlopen):
            twd._get_session_activity_history("cc-9930", limit=-5)
        assert "limit=1" in captured["url"]

    def test_limit_over_50_clamps_to_50(self):
        captured = {}

        def capturing_urlopen(req, timeout=5):
            captured["url"] = req.full_url
            return _FakeResp([])

        with patch("urllib.request.urlopen", capturing_urlopen):
            twd._get_session_activity_history("cc-9930", limit=9999)
        assert "limit=50" in captured["url"]

    def test_non_numeric_limit_defaults_to_ten(self):
        captured = {}

        def capturing_urlopen(req, timeout=5):
            captured["url"] = req.full_url
            return _FakeResp([])

        with patch("urllib.request.urlopen", capturing_urlopen):
            twd._get_session_activity_history("cc-9930", limit="bogus")
        assert "limit=10" in captured["url"]


class TestCacheKeying:
    def setup_method(self):
        _clear_cache()

    def test_same_args_hit_cache(self):
        calls = {"n": 0}

        def counting(req, timeout=5):
            calls["n"] += 1
            return _FakeResp([])

        with patch("urllib.request.urlopen", counting):
            twd._get_session_activity_history("cc-9930", limit=5, lookback_hours=6)
            twd._get_session_activity_history("cc-9930", limit=5, lookback_hours=6)
        assert calls["n"] == 1

    def test_different_session_busts_cache(self):
        calls = {"n": 0}

        def counting(req, timeout=5):
            calls["n"] += 1
            return _FakeResp([])

        with patch("urllib.request.urlopen", counting):
            twd._get_session_activity_history("cc-A", limit=5)
            twd._get_session_activity_history("cc-B", limit=5)
        assert calls["n"] == 2

    def test_different_lookback_busts_cache(self):
        calls = {"n": 0}

        def counting(req, timeout=5):
            calls["n"] += 1
            return _FakeResp([])

        with patch("urllib.request.urlopen", counting):
            twd._get_session_activity_history("cc-X", limit=5, lookback_hours=1)
            twd._get_session_activity_history("cc-X", limit=5, lookback_hours=24)
        assert calls["n"] == 2


class TestFilesFieldHandling:
    def setup_method(self):
        _clear_cache()

    def test_files_null_is_coerced_to_empty_list(self):
        now = datetime.now(timezone.utc)
        rows = [
            {
                "id": "x",
                "session_id": "cc-1",
                "item_type": "fix",
                "title": "t",
                "project": "p",
                "company": "c",
                "commit_sha": "sha",
                "test_status": "tested",
                "files": None,
                "created_at": _iso(now),
            }
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            result = twd._get_session_activity_history("cc-1")
        assert result[0]["files"] == []

    def test_files_non_list_defaults_to_empty(self):
        now = datetime.now(timezone.utc)
        rows = [
            {
                "id": "x",
                "session_id": "cc-1",
                "item_type": "fix",
                "title": "t",
                "project": "p",
                "company": "c",
                "commit_sha": "sha",
                "test_status": "tested",
                "files": "not-a-list",
                "created_at": _iso(now),
            }
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            result = twd._get_session_activity_history("cc-1")
        assert result[0]["files"] == []
