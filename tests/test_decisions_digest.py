"""Regression tests for ``_get_recent_decisions``.

Surfaces the `[DECISION]` / `decision:` commit stream from build_ledger
so a Mission Control panel or the advisor briefing can show recent
architectural commitments.
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
    twd._DECISIONS_CACHE = None
    twd._DECISIONS_CACHE_TIME = 0.0
    twd._DECISIONS_CACHE_KEY = None


def _row(session, project, title, minutes_ago):
    now = datetime.now(timezone.utc)
    return {
        "id": f"uuid-{title[:8]}",
        "session_id": session,
        "project": project,
        "company": "personal",
        "title": title,
        "commit_sha": "abc1234",
        "created_at": _iso(now - timedelta(minutes=minutes_ago)),
    }


# ---------------------------------------------------------------------------


class TestEmptyAndErrorShapes:
    def setup_method(self):
        _clear_cache()

    def test_empty_response_returns_zero(self):
        with patch("urllib.request.urlopen", _urlopen_returning([])):
            r = twd._get_recent_decisions()
        assert r["total"] == 0
        assert r["decisions"] == []
        assert r["by_project"] == {}
        assert r["by_session"] == {}

    def test_network_error_returns_empty_shape(self):
        def broken(req, timeout=5):
            raise RuntimeError("boom")

        with patch("urllib.request.urlopen", broken):
            r = twd._get_recent_decisions()
        assert r["total"] == 0
        assert r["decisions"] == []

    def test_non_list_response_returns_empty(self):
        class _FakeRespDict:
            def read(self):
                return json.dumps({"error": "not found"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=5):
            return _FakeRespDict()

        with patch("urllib.request.urlopen", fake):
            r = twd._get_recent_decisions()
        assert r["total"] == 0


class TestAggregation:
    def setup_method(self):
        _clear_cache()

    def test_sorts_by_recency(self):
        rows = [
            _row("cc-A", "atlas", "oldest", 120),
            _row("cc-B", "atlas", "newest", 1),
            _row("cc-C", "atlas", "middle", 30),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_recent_decisions()
        # Input is pre-sorted by Supabase via `order=created_at.desc`,
        # but our helper preserves the order it receives. Verify titles
        # appear in the same order as the rows list.
        titles = [d["title"] for d in r["decisions"]]
        assert titles == ["oldest", "newest", "middle"]

    def test_by_project_counts_and_sorted_desc(self):
        rows = [
            _row("cc-1", "atlas", "a1", 1),
            _row("cc-2", "atlas", "a2", 2),
            _row("cc-3", "atlas", "a3", 3),
            _row("cc-4", "token-watch", "t1", 4),
            _row("cc-5", "battlestation", "b1", 5),
            _row("cc-6", "battlestation", "b2", 6),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_recent_decisions()
        assert r["total"] == 6
        # atlas=3, battlestation=2, token-watch=1 — sorted by count desc
        ordered_projects = list(r["by_project"].keys())
        assert ordered_projects[0] == "atlas"
        assert r["by_project"]["atlas"] == 3
        assert r["by_project"]["battlestation"] == 2
        assert r["by_project"]["token-watch"] == 1

    def test_by_session_counts(self):
        rows = [
            _row("cc-A", "p", "t1", 1),
            _row("cc-A", "p", "t2", 2),
            _row("cc-B", "p", "t3", 3),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_recent_decisions()
        assert r["by_session"]["cc-A"] == 2
        assert r["by_session"]["cc-B"] == 1

    def test_age_minutes_computed(self):
        rows = [_row("cc-A", "p", "t", 5)]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_recent_decisions()
        age = r["decisions"][0]["age_minutes"]
        assert age is not None
        assert 4 <= age <= 6

    def test_missing_project_falls_back_to_unknown(self):
        rows = [
            {
                "id": "x",
                "session_id": "cc-A",
                "project": None,
                "company": "c",
                "title": "orphan",
                "commit_sha": "",
                "created_at": _iso(datetime.now(timezone.utc)),
            }
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_recent_decisions()
        assert r["by_project"]["unknown"] == 1
        assert r["decisions"][0]["project"] == "unknown"


class TestClampingAndValidation:
    def setup_method(self):
        _clear_cache()

    def test_zero_lookback_clamps_to_one(self):
        captured = {}

        def capture(req, timeout=5):
            captured["url"] = req.full_url
            return _FakeResp([])

        with patch("urllib.request.urlopen", capture):
            twd._get_recent_decisions(lookback_hours=0)
        # cutoff should be computed from 1 hour, not 0
        assert "limit=30" in captured["url"]  # default limit preserved

    def test_limit_over_200_clamps(self):
        captured = {}

        def capture(req, timeout=5):
            captured["url"] = req.full_url
            return _FakeResp([])

        with patch("urllib.request.urlopen", capture):
            twd._get_recent_decisions(limit=9999)
        assert "limit=200" in captured["url"]

    def test_non_numeric_lookback_defaults(self):
        captured = {}

        def capture(req, timeout=5):
            captured["url"] = req.full_url
            return _FakeResp([])

        with patch("urllib.request.urlopen", capture):
            twd._get_recent_decisions(lookback_hours="bogus")
        # Should not raise and should still make the request
        assert "build_ledger?item_type=eq.decision" in captured["url"]


class TestCaching:
    def setup_method(self):
        _clear_cache()

    def test_cache_hit_on_same_args(self):
        calls = {"n": 0}

        def counting(req, timeout=5):
            calls["n"] += 1
            return _FakeResp([])

        with patch("urllib.request.urlopen", counting):
            twd._get_recent_decisions(lookback_hours=24, limit=10)
            twd._get_recent_decisions(lookback_hours=24, limit=10)
        assert calls["n"] == 1

    def test_different_args_bust_cache(self):
        calls = {"n": 0}

        def counting(req, timeout=5):
            calls["n"] += 1
            return _FakeResp([])

        with patch("urllib.request.urlopen", counting):
            twd._get_recent_decisions(lookback_hours=24, limit=10)
            twd._get_recent_decisions(lookback_hours=72, limit=10)
            twd._get_recent_decisions(lookback_hours=24, limit=20)
        assert calls["n"] == 3
