"""Regression tests for ``_classify_test_status`` and ``_get_test_status_rollup``.

Focus areas:

* classifier must put ``"untested"`` into the untested bucket even
  though the string contains ``"tested"`` as a substring (the bug that
  flipped live ``tested_pct`` from ~22% to ~98%)
* ``tested_pct`` excludes skipped entries from the denominator
* per-project summary rows include tested + untested + skipped counts
* oldest untested are sorted age-desc, newest items not in the list
* cache keyed on ``(lookback_hours, oldest_limit)``
* error paths return the empty shape
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import token_watch_data as twd


def _iso(dt):
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
    def fake(req, timeout=5):
        return _FakeResp(rows)
    return fake


def _clear_cache():
    twd._TEST_ROLLUP_CACHE = None
    twd._TEST_ROLLUP_CACHE_TIME = 0.0
    twd._TEST_ROLLUP_CACHE_KEY = None


def _row(project, title, status, hours_ago):
    now = datetime.now(timezone.utc)
    return {
        "id": f"uuid-{title[:6]}",
        "project": project,
        "title": title,
        "test_status": status,
        "created_at": _iso(now - timedelta(hours=hours_ago)),
    }


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------


class TestClassifier:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # tested variants
            ("tested", "tested"),
            ("verified", "tested"),
            ("passed", "tested"),
            ("passing", "tested"),
            ("ci_green", "tested"),
            ("all_pass", "tested"),
            ("tests_passed", "tested"),
            ("succeeded", "tested"),
            # the critical regression: "untested" must not fall into "tested"
            # just because the substring match finds "tested"
            ("untested", "untested"),
            ("UNTESTED", "untested"),
            # other untested variants
            ("failed", "untested"),
            ("fail", "untested"),
            ("ci_pending", "untested"),
            ("pending", "untested"),
            ("pushed_to_queue", "untested"),
            ("blocked", "untested"),
            # skipped
            ("skipped", "skipped"),
            ("n/a", "skipped"),
            # empty / None defaults
            ("", "untested"),
            (None, "untested"),
            ("   ", "untested"),
        ],
    )
    def test_classifier(self, raw, expected):
        assert twd._classify_test_status(raw) == expected


# ---------------------------------------------------------------------------
# rollup aggregation
# ---------------------------------------------------------------------------


class TestRollupEmpty:
    def setup_method(self):
        _clear_cache()

    def test_empty_response(self):
        with patch("urllib.request.urlopen", _urlopen_returning([])):
            r = twd._get_test_status_rollup()
        assert r["total"] == 0
        assert r["by_bucket"] == {"tested": 0, "untested": 0, "skipped": 0}
        assert r["tested_pct"] == 0.0
        assert r["oldest_untested"] == []

    def test_network_error_returns_empty(self):
        def broken(req, timeout=5):
            raise RuntimeError("boom")

        with patch("urllib.request.urlopen", broken):
            r = twd._get_test_status_rollup()
        assert r["total"] == 0
        assert r["oldest_untested"] == []


class TestRollupAggregation:
    def setup_method(self):
        _clear_cache()

    def test_tested_pct_excludes_skipped(self):
        """skipped rows should not inflate or deflate the ratio."""
        rows = [
            _row("atlas", "a", "tested", 1),
            _row("atlas", "b", "untested", 1),
            _row("atlas", "c", "skipped", 1),
            _row("atlas", "d", "skipped", 1),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup()
        # 1 tested / 2 non-skipped = 50%
        assert r["tested_pct"] == 50.0
        assert r["by_bucket"] == {"tested": 1, "untested": 1, "skipped": 2}

    def test_untested_substring_no_longer_traps(self):
        """The regression bug: 'untested' must bucket as untested."""
        rows = [_row("p", "t1", "untested", 1) for _ in range(10)]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup()
        assert r["by_bucket"]["untested"] == 10
        assert r["by_bucket"]["tested"] == 0
        assert r["tested_pct"] == 0.0

    def test_mixed_variants_classified(self):
        rows = [
            _row("x", "t1", "tested", 1),
            _row("x", "t2", "verified", 1),
            _row("x", "t3", "passing", 1),
            _row("x", "t4", "ci_green", 1),
            _row("x", "t5", "untested", 1),
            _row("x", "t6", "failed", 1),
            _row("x", "t7", "pushed_to_queue", 1),
            _row("x", "t8", "skipped", 1),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup()
        assert r["by_bucket"]["tested"] == 4
        assert r["by_bucket"]["untested"] == 3
        assert r["by_bucket"]["skipped"] == 1
        # 4 tested / 7 non-skipped ≈ 57.1%
        assert r["tested_pct"] == pytest.approx(57.1, abs=0.1)

    def test_by_project_sorted_by_untested_desc(self):
        rows = [
            *[_row("atlas", f"a{i}", "untested", 1) for i in range(5)],
            *[_row("token-watch", f"t{i}", "untested", 1) for i in range(2)],
            *[_row("battlestation", f"b{i}", "untested", 1) for i in range(4)],
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup()
        projects = list(r["by_project"].keys())
        assert projects[0] == "atlas"
        assert projects[1] == "battlestation"
        assert projects[2] == "token-watch"

    def test_raw_status_preserved(self):
        rows = [
            _row("p", "a", "ci_green", 1),
            _row("p", "b", "ci_green", 1),
            _row("p", "c", "untested", 1),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup()
        assert r["by_status_raw"]["ci_green"] == 2
        assert r["by_status_raw"]["untested"] == 1


class TestOldestUntested:
    def setup_method(self):
        _clear_cache()

    def test_oldest_first_age_sort(self):
        rows = [
            _row("p", "old", "untested", 24),
            _row("p", "middle", "untested", 6),
            _row("p", "young", "untested", 1),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup()
        titles = [e["title"] for e in r["oldest_untested"]]
        assert titles == ["old", "middle", "young"]

    def test_oldest_limit_clamps_list(self):
        rows = [_row("p", f"t{i}", "untested", 20 - i) for i in range(15)]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup(oldest_limit=5)
        assert len(r["oldest_untested"]) == 5

    def test_tested_rows_not_in_oldest_list(self):
        rows = [
            _row("p", "old_tested", "tested", 48),
            _row("p", "old_untested", "untested", 24),
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_test_status_rollup()
        titles = [e["title"] for e in r["oldest_untested"]]
        assert "old_tested" not in titles
        assert "old_untested" in titles


class TestCaching:
    def setup_method(self):
        _clear_cache()

    def test_same_args_hit_cache(self):
        calls = {"n": 0}

        def counting(req, timeout=5):
            calls["n"] += 1
            return _FakeResp([])

        with patch("urllib.request.urlopen", counting):
            twd._get_test_status_rollup(lookback_hours=48, oldest_limit=10)
            twd._get_test_status_rollup(lookback_hours=48, oldest_limit=10)
        assert calls["n"] == 1

    def test_different_args_bust_cache(self):
        calls = {"n": 0}

        def counting(req, timeout=5):
            calls["n"] += 1
            return _FakeResp([])

        with patch("urllib.request.urlopen", counting):
            twd._get_test_status_rollup(lookback_hours=24, oldest_limit=10)
            twd._get_test_status_rollup(lookback_hours=48, oldest_limit=10)
            twd._get_test_status_rollup(lookback_hours=24, oldest_limit=5)
        assert calls["n"] == 3
