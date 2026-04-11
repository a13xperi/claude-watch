"""Regression tests for ``_get_wire_reliability``.

Born from the cc-18721 dead-advisor bug: T10 wired 45 minutes of
status/ack messages to a session that had been dead for 20+ minutes.
Every message landed in session_messages with read=false, and the
worker had no signal. This helper surfaces that failure mode as a
numeric reliability score + per-recipient roll-up.

Tests cover:
* empty state (no outbound messages)
* full reliability (everyone reading)
* dead-recipient detection (>=3 sent, 0 read → likely_dead=True)
* mixed recipients sorted dead-first
* exclusion of self-to-self messages
* network errors fall through to the empty payload shape
* cache hit on repeated same-args call
"""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

import token_watch_data as twd


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
    twd._WIRE_HEALTH_CACHE = None
    twd._WIRE_HEALTH_CACHE_TIME = 0.0
    twd._WIRE_HEALTH_CACHE_KEY = None


# ---------------------------------------------------------------------------


class TestEmptyState:
    def setup_method(self):
        _clear_cache()

    def test_no_outbound_returns_empty_shape(self):
        with patch("urllib.request.urlopen", _urlopen_returning([])):
            result = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        assert result["sender"] == "cc-9930"
        assert result["window_minutes"] == 15
        assert result["total_sent"] == 0
        assert result["read"] == 0
        assert result["unread"] == 0
        assert result["reliability_pct"] == 0.0
        assert result["by_recipient"] == []

    def test_network_error_returns_empty_shape(self):
        def broken(req, timeout=5):
            raise urllib.error.URLError("down")

        with patch("urllib.request.urlopen", broken):
            result = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        assert result["total_sent"] == 0
        assert result["by_recipient"] == []


class TestFullReliability:
    def setup_method(self):
        _clear_cache()

    def test_all_messages_read(self):
        rows = [
            {"id": "1", "to_session": "cc-advisor", "read": True,  "msg_type": "status"},
            {"id": "2", "to_session": "cc-advisor", "read": True,  "msg_type": "status"},
            {"id": "3", "to_session": "cc-peer",    "read": True,  "msg_type": "status"},
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        assert r["total_sent"] == 3
        assert r["read"] == 3
        assert r["unread"] == 0
        assert r["reliability_pct"] == 100.0
        # No likely_dead flags
        for e in r["by_recipient"]:
            assert e["likely_dead"] is False


class TestDeadRecipientDetection:
    def setup_method(self):
        _clear_cache()

    def test_three_unread_in_a_row_flags_dead(self):
        """The exact bug: 3+ messages to the same recipient, all unread."""
        rows = [
            {"id": "1", "to_session": "cc-18721", "read": False, "msg_type": "status"},
            {"id": "2", "to_session": "cc-18721", "read": False, "msg_type": "status"},
            {"id": "3", "to_session": "cc-18721", "read": False, "msg_type": "status"},
            {"id": "4", "to_session": "cc-46255", "read": True,  "msg_type": "status"},
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)

        assert r["total_sent"] == 4
        assert r["read"] == 1
        assert r["unread"] == 3
        assert r["reliability_pct"] == 25.0

        dead = [e for e in r["by_recipient"] if e["likely_dead"]]
        assert len(dead) == 1
        assert dead[0]["to"] == "cc-18721"
        assert dead[0]["sent"] == 3
        assert dead[0]["read"] == 0

    def test_two_unread_not_enough_for_dead_flag(self):
        """Threshold is >=3 sent with 0 read. Two unread = inconclusive."""
        rows = [
            {"id": "1", "to_session": "cc-who", "read": False, "msg_type": "status"},
            {"id": "2", "to_session": "cc-who", "read": False, "msg_type": "status"},
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        assert r["by_recipient"][0]["likely_dead"] is False

    def test_dead_recipients_sorted_first(self):
        """likely_dead recipients must appear at the top of by_recipient."""
        rows = [
            # cc-live gets 5 healthy messages
            *[
                {"id": f"l{i}", "to_session": "cc-live", "read": True, "msg_type": "status"}
                for i in range(5)
            ],
            # cc-dead gets 3 unread messages
            *[
                {"id": f"d{i}", "to_session": "cc-dead", "read": False, "msg_type": "status"}
                for i in range(3)
            ],
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        recipients = r["by_recipient"]
        assert recipients[0]["to"] == "cc-dead", (
            "likely_dead recipient must be first in the list"
        )
        assert recipients[0]["likely_dead"] is True
        assert recipients[1]["to"] == "cc-live"
        assert recipients[1]["likely_dead"] is False


class TestEdgeCases:
    def setup_method(self):
        _clear_cache()

    def test_self_to_self_excluded(self):
        """Messages where sender == recipient should not count."""
        rows = [
            {"id": "1", "to_session": "cc-9930", "read": False, "msg_type": "status"},
            {"id": "2", "to_session": "cc-other", "read": True,  "msg_type": "status"},
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        assert r["total_sent"] == 1
        assert all(e["to"] != "cc-9930" for e in r["by_recipient"])

    def test_missing_to_session_skipped(self):
        rows = [
            {"id": "1", "to_session": "", "read": True, "msg_type": "status"},
            {"id": "2", "to_session": None, "read": True, "msg_type": "status"},
            {"id": "3", "to_session": "cc-ok", "read": True, "msg_type": "status"},
        ]
        with patch("urllib.request.urlopen", _urlopen_returning(rows)):
            r = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        assert r["total_sent"] == 1
        assert r["by_recipient"][0]["to"] == "cc-ok"


class TestCaching:
    def setup_method(self):
        _clear_cache()

    def test_second_call_hits_cache(self):
        rows = [{"id": "1", "to_session": "cc-a", "read": True, "msg_type": "status"}]
        call_count = {"n": 0}

        def counting_urlopen(req, timeout=5):
            call_count["n"] += 1
            return _FakeResp(rows)

        with patch("urllib.request.urlopen", counting_urlopen):
            r1 = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
            r2 = twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
        assert r1 == r2
        assert call_count["n"] == 1, "second call should hit the cache"

    def test_different_args_bust_cache(self):
        rows = [{"id": "1", "to_session": "cc-a", "read": True, "msg_type": "status"}]
        call_count = {"n": 0}

        def counting_urlopen(req, timeout=5):
            call_count["n"] += 1
            return _FakeResp(rows)

        with patch("urllib.request.urlopen", counting_urlopen):
            twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=15)
            twd._get_wire_reliability(sender_sid="cc-9930", lookback_minutes=30)
        assert call_count["n"] == 2, "different lookback should re-fetch"
