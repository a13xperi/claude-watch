"""Regression tests for ``_get_engine_breakdown``.

Deepens the #112951 attribution fix with a per-engine (model) roll-up.
Pins:

* the output shape (``total_pct``, ``account``, ``engines`` list)
* sort order (descending by pct, alphabetical tie-break)
* engine normalisation (``opus:1m`` / ``claude-opus-4-6`` all collapse to ``"opus"``)
* caching (same-account call hits the cache, account-change busts it)
* aggregation — sessions with the same engine combine pct_used + tool_count
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import token_watch_data as twd


def _clear_cache():
    twd._ENGINE_CACHE = None
    twd._ENGINE_CACHE_TIME = 0.0
    twd._ENGINE_CACHE_ACCOUNT = None


def _fake_attribution(sessions):
    """Build a minimal attribution dict matching the real shape."""
    return {
        "total_used_pct": sum(s.get("pct_used", 0) for s in sessions),
        "unaccounted_pct": 0.0,
        "rolled_off_pct": 0.0,
        "sessions": sessions,
        "unaccounted_candidates": [],
    }


class TestEngineNormalisation:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("opus", "opus"),
            ("opus:1m", "opus"),
            ("claude-opus-4-6", "opus"),
            ("claude-opus-4-6[1m]", "opus"),
            ("sonnet", "sonnet"),
            ("claude-sonnet-4-6", "sonnet"),
            ("haiku", "haiku"),
            ("claude-haiku-4-5-20251001", "haiku"),
            ("gpt-5.4", "gpt"),
            ("codex", "gpt"),
            ("gemini-pro", "gemini"),
            ("grok-4", "grok"),
            ("", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_canonical_labels(self, model, expected):
        assert twd._normalise_engine(model) == expected


class TestEngineBreakdownShape:
    def setup_method(self):
        _clear_cache()

    def test_empty_attribution_returns_empty_breakdown(self):
        with patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_get_token_attribution", return_value={}):
            result = twd._get_engine_breakdown()
        assert result == {"total_pct": 0.0, "account": "B", "engines": []}

    def test_single_engine(self):
        sessions = [
            {"session_id": "cc-1", "pct_used": 20.5, "model": "opus", "tool_count": 10},
            {"session_id": "cc-2", "pct_used": 5.0, "model": "opus:1m", "tool_count": 4},
        ]
        with patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_get_token_attribution", return_value=_fake_attribution(sessions)):
            result = twd._get_engine_breakdown()
        assert result["account"] == "B"
        assert len(result["engines"]) == 1
        entry = result["engines"][0]
        assert entry["engine"] == "opus"
        assert entry["pct"] == pytest.approx(25.5, abs=0.01)
        assert entry["sessions"] == 2
        assert entry["tools"] == 14
        assert result["total_pct"] == pytest.approx(25.5, abs=0.01)

    def test_multi_engine_sorted_desc(self):
        sessions = [
            {"session_id": "cc-a", "pct_used": 10, "model": "sonnet", "tool_count": 5},
            {"session_id": "cc-b", "pct_used": 30, "model": "opus", "tool_count": 20},
            {"session_id": "cc-c", "pct_used": 5, "model": "haiku", "tool_count": 2},
        ]
        with patch.object(twd, "_get_active_account", return_value=("A", "", "")), \
             patch.object(twd, "_get_token_attribution", return_value=_fake_attribution(sessions)):
            result = twd._get_engine_breakdown()
        engines = [e["engine"] for e in result["engines"]]
        assert engines == ["opus", "sonnet", "haiku"], f"wrong sort order: {engines}"
        assert result["total_pct"] == pytest.approx(45.0, abs=0.01)

    def test_unknown_model_bucket(self):
        sessions = [
            {"session_id": "cc-x", "pct_used": 3.0, "model": "", "tool_count": 1},
            {"session_id": "cc-y", "pct_used": 2.0, "model": None, "tool_count": 2},
        ]
        with patch.object(twd, "_get_active_account", return_value=("C", "", "")), \
             patch.object(twd, "_get_token_attribution", return_value=_fake_attribution(sessions)):
            result = twd._get_engine_breakdown()
        assert len(result["engines"]) == 1
        assert result["engines"][0]["engine"] == "unknown"
        assert result["engines"][0]["sessions"] == 2
        assert result["engines"][0]["tools"] == 3


class TestEngineBreakdownCache:
    def setup_method(self):
        _clear_cache()

    def test_same_account_hits_cache(self):
        call_count = {"n": 0}

        def counting_attr():
            call_count["n"] += 1
            return _fake_attribution(
                [{"session_id": "cc-1", "pct_used": 10, "model": "opus", "tool_count": 3}]
            )

        with patch.object(twd, "_get_active_account", return_value=("A", "", "")), \
             patch.object(twd, "_get_token_attribution", side_effect=counting_attr):
            twd._get_engine_breakdown()
            twd._get_engine_breakdown()
        assert call_count["n"] == 1, "second call should hit the cache"

    def test_account_change_busts_cache(self):
        call_count = {"n": 0}

        def counting_attr():
            call_count["n"] += 1
            return _fake_attribution(
                [{"session_id": "cc-1", "pct_used": 10, "model": "opus", "tool_count": 3}]
            )

        with patch.object(twd, "_get_token_attribution", side_effect=counting_attr):
            with patch.object(twd, "_get_active_account", return_value=("A", "", "")):
                twd._get_engine_breakdown()
            with patch.object(twd, "_get_active_account", return_value=("B", "", "")):
                twd._get_engine_breakdown()
        assert call_count["n"] == 2, "account change should bust the cache"
