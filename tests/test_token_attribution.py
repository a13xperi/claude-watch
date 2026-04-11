"""Regression tests for ``_get_token_attribution`` (bug #112951).

The reported bug: Engine Management "Used%" column showed OpenClaw
sessions at +64-65% while the account's own 5h window was only at 32%.
Root cause: ``_get_token_attribution`` called ``_load_ledger()`` with no
account filter, so ledger entries from multiple accounts were mixed
into a single consecutive-delta pass. A session tagged with account B
inherited a five_pct jump from account A's window, producing impossible
attribution numbers.

Fix: filter the ledger by active account; invalidate the attribution
cache when the active account changes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import token_watch_data as twd


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clear_attr_cache():
    twd._attribution_cache = None
    twd._attribution_cache_time = 0.0
    twd._attribution_cache_account = None


# ---------------------------------------------------------------------------


class TestLoadLedgerIsAccountFiltered:
    """The one-line regression guard: _get_token_attribution must pass
    ``account=<current>`` when loading the ledger. Without this, cross-account
    ledger entries leak into the consecutive-delta computation.
    """

    def setup_method(self):
        _clear_attr_cache()

    def test_load_ledger_called_with_current_account(self):
        """_get_token_attribution must load the ledger filtered by active account."""
        call_args = {}

        def fake_loader(last_n=None, account=None):
            call_args["account"] = account
            return []

        now = _now()
        with patch.object(twd, "_load_ledger", side_effect=fake_loader), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(
                 twd, "_current_pct",
                 return_value=(30, 10, _iso(now + timedelta(hours=2)), _iso(now + timedelta(days=3)))
             ):
            twd._get_token_attribution()

        assert call_args.get("account") == "B", (
            f"_load_ledger must be called with account='B' but got account={call_args.get('account')!r}"
        )


class TestCacheInvalidatesOnAccountChange:
    def setup_method(self):
        _clear_attr_cache()

    def test_switch_account_busts_cache(self):
        """After /switch-account the cached attribution must be re-computed."""
        now = _now()
        window_start = now - timedelta(minutes=30)

        # Minimal 2-entry ledger so _get_token_attribution populates the cache
        # instead of early-returning on empty window_entries.
        entries = [
            {
                "type": "tool_use",
                "ts": _iso(window_start + timedelta(minutes=5)),
                "five_pct": 10,
                "session": "cc-1111",
                "directive": "fake",
                "output_tokens": 100,
                "model": "claude-opus-4-6",
                "tool": "Edit",
                "account": "A",
            },
            {
                "type": "tool_use",
                "ts": _iso(window_start + timedelta(minutes=20)),
                "five_pct": 30,
                "session": "cc-1111",
                "directive": "fake",
                "output_tokens": 200,
                "model": "claude-opus-4-6",
                "tool": "Write",
                "account": "A",
            },
        ]

        call_count = {"n": 0}

        def counting_loader(last_n=None, account=None):
            call_count["n"] += 1
            return [e for e in entries if account is None or e.get("account") == account]

        pct_values = (30, 10, _iso(now + timedelta(hours=2)), _iso(now + timedelta(days=3)))

        # First call on account A — populates cache
        with patch.object(twd, "_load_ledger", side_effect=counting_loader), \
             patch.object(twd, "_get_active_account", return_value=("A", "", "")), \
             patch.object(twd, "_current_pct", return_value=pct_values):
            twd._get_token_attribution()
        first_count = call_count["n"]
        assert first_count >= 1

        # Second call on SAME account — cache should be used (no new loader call)
        with patch.object(twd, "_load_ledger", side_effect=counting_loader), \
             patch.object(twd, "_get_active_account", return_value=("A", "", "")), \
             patch.object(twd, "_current_pct", return_value=pct_values):
            twd._get_token_attribution()
        second_count = call_count["n"]
        assert second_count == first_count, "same-account call should hit the cache"

        # Third call on DIFFERENT account — cache must be busted
        with patch.object(twd, "_load_ledger", side_effect=counting_loader), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_current_pct", return_value=pct_values):
            twd._get_token_attribution()
        third_count = call_count["n"]
        assert third_count > second_count, (
            "account change must invalidate the attribution cache — "
            f"loader call count stuck at {second_count} after switch from A to B"
        )


class TestAttributionPctWithinRange:
    """With the account filter in place, individual session pct_used values
    must be bounded by the total five_pct for that account. Before the fix,
    session values could exceed the window total because cross-account deltas
    inflated them.
    """

    def setup_method(self):
        _clear_attr_cache()

    def test_session_pct_does_not_exceed_current_five(self):
        now = _now()
        window_start = now - timedelta(minutes=30)

        # Two tool_use entries both tagged with account B.
        # five_pct progresses 0 -> 30. Session "cc-1111" owns the delta.
        ledger_entries = [
            {
                "type": "tool_use",
                "ts": _iso(window_start + timedelta(minutes=5)),
                "five_pct": 0,
                "session": "cc-1111",
                "directive": "fake session 1",
                "output_tokens": 100,
                "model": "claude-opus-4-6",
                "tool": "Edit",
                "account": "B",
            },
            {
                "type": "tool_use",
                "ts": _iso(window_start + timedelta(minutes=25)),
                "five_pct": 30,
                "session": "cc-1111",
                "directive": "fake session 1",
                "output_tokens": 200,
                "model": "claude-opus-4-6",
                "tool": "Write",
                "account": "B",
            },
        ]

        # Poisoned entries from account A — must NOT appear in results.
        # If the account filter is broken, these would push pct_used > 30.
        poisoned = [
            {
                "type": "tool_use",
                "ts": _iso(window_start + timedelta(minutes=10)),
                "five_pct": 80,  # account A at 80%
                "session": "cc-1111",
                "directive": "fake session 1",
                "output_tokens": 9999,
                "model": "claude-opus-4-6",
                "tool": "Bash",
                "account": "A",  # wrong account
            },
        ]

        def loader(last_n=None, account=None):
            # Honour the real filter semantics — if account filter is absent,
            # return EVERYTHING (poisoned + clean). If it is present, filter.
            all_entries = ledger_entries + poisoned
            if account is None:
                return all_entries
            return [e for e in all_entries if e.get("account") == account]

        with patch.object(twd, "_load_ledger", side_effect=loader), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(
                 twd, "_current_pct",
                 return_value=(30, 10, _iso(now + timedelta(hours=2)), _iso(now + timedelta(days=3)))
             ):
            result = twd._get_token_attribution()

        assert result, "attribution result should not be empty"
        sessions = result.get("sessions", [])
        assert sessions, "sessions list should not be empty"

        # The 80% poisoned value from account A must be filtered out.
        # cc-1111's attribution should match the account-B delta (≤30).
        for s in sessions:
            assert s["pct_used"] <= 30.0 + 0.1, (
                f"session {s['session_id']} pct_used={s['pct_used']} "
                f"exceeds current_five=30 — cross-account poisoning leaked in"
            )
