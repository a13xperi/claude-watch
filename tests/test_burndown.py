"""Regression tests for ``_get_burndown_data`` (bug #127277).

The reported bug: burndown % showed **144%** then jumped to **14%** within
seconds. Both values are outside the legal [0, 100] range and the jump is
a staleness artefact around an account switch.

These tests pin the defensive invariants introduced in the fix:

1. ``remaining_pct`` is always clamped to ``[0, 100]`` regardless of the
   upstream ``_current_pct()`` reading.
2. ``current_rate`` is never negative — a window reset mid-interval used
   to emit a negative rate, which downstream projection code inverted.
3. ``projected_remaining_at_reset`` is clamped to ``[0, 100]``.
4. When the account-filtered ledger is empty (normal right after
   ``/switch-account``), ``raw_points`` is seeded with the live remaining
   instead of a hardcoded 100% that later flips to the real value —
   that flip is the exact "144% → 14%" symptom.
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


def _reset_in(minutes: float) -> str:
    return _iso(_now() + timedelta(minutes=minutes))


def _mock_current_pct(five, reset_offset_min=240):
    """Return a patcher that makes ``_current_pct`` return deterministic values."""
    return patch.object(
        twd,
        "_current_pct",
        return_value=(five, 20, _reset_in(reset_offset_min), _reset_in(reset_offset_min + 60)),
    )


def _clear_burndown_cache():
    """The burndown has a 30s cache keyed on (time, account). Clear it between tests."""
    twd._burndown_cache = None
    twd._burndown_cache_time = 0.0
    twd._burndown_cache_account = None


# ---------------------------------------------------------------------------


class TestRemainingPctClamp:
    def setup_method(self):
        _clear_burndown_cache()

    def test_normal_value_passes_through(self):
        with _mock_current_pct(60), \
             patch.object(twd, "_load_ledger", return_value=[]), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")):
            data = twd._get_burndown_data()
        assert data["remaining_pct"] == pytest.approx(40.0, abs=0.1)

    def test_five_over_hundred_clamps_to_zero(self):
        """A bogus upstream five=144 used to render as remaining=-44 → '144% left' wraparound."""
        with _mock_current_pct(144), \
             patch.object(twd, "_load_ledger", return_value=[]), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")):
            data = twd._get_burndown_data()
        assert data["remaining_pct"] == 0.0, (
            "remaining_pct must be clamped to 0 when upstream five exceeds 100"
        )

    def test_negative_five_clamps_to_hundred(self):
        """A negative five (e.g. from a stale reset delta) must not produce >100 remaining."""
        with _mock_current_pct(-25), \
             patch.object(twd, "_load_ledger", return_value=[]), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")):
            data = twd._get_burndown_data()
        assert data["remaining_pct"] == 100.0, (
            "remaining_pct must be clamped to 100 when upstream five is negative"
        )

    def test_five_exactly_hundred(self):
        with _mock_current_pct(100), \
             patch.object(twd, "_load_ledger", return_value=[]), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")):
            data = twd._get_burndown_data()
        assert data["remaining_pct"] == 0.0


class TestCurrentRateNonNegative:
    def setup_method(self):
        _clear_burndown_cache()

    def _build_entries(self, pairs):
        """pairs = [(minutes_before_now, five_pct), ...]. Returns ledger entry dicts."""
        now = _now()
        return [
            {
                "type": "tool_use",
                "ts": _iso(now - timedelta(minutes=m)),
                "five_pct": pct,
                "account": "B",
            }
            for m, pct in pairs
        ]

    def test_window_reset_does_not_produce_negative_rate(self):
        """If a sample mid-window has LOWER five (i.e. a reset), rate must clamp at 0."""
        # Scenario: 5 minutes ago five=90 (remaining=10), now five=20 (remaining=80 — reset)
        entries = self._build_entries([(5, 90), (1, 20)])
        with _mock_current_pct(20), \
             patch.object(twd, "_load_ledger", return_value=entries), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")):
            data = twd._get_burndown_data()
        assert data["current_rate"] >= 0, (
            f"current_rate={data['current_rate']} must be non-negative even on window reset"
        )

    def test_normal_consumption_produces_positive_rate(self):
        """Monotonically consumed ledger → positive rate (sanity check)."""
        # 8 min ago: five=40 (remaining=60); 1 min ago: five=60 (remaining=40)
        entries = self._build_entries([(8, 40), (1, 60)])
        with _mock_current_pct(60), \
             patch.object(twd, "_load_ledger", return_value=entries), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")):
            data = twd._get_burndown_data()
        assert data["current_rate"] > 0, "normal consumption should produce positive rate"


class TestProjectedRemainingClamp:
    def setup_method(self):
        _clear_burndown_cache()

    def test_projected_stays_within_range(self):
        """Even with spiky rate + long remaining window, projection stays in [0, 100]."""
        now = _now()
        entries = [
            {
                "type": "tool_use",
                "ts": _iso(now - timedelta(minutes=m)),
                "five_pct": pct,
                "account": "B",
            }
            for m, pct in [(9, 10), (1, 90)]  # high burn rate
        ]
        with _mock_current_pct(90, reset_offset_min=240), \
             patch.object(twd, "_load_ledger", return_value=entries), \
             patch.object(twd, "_get_active_account", return_value=("B", "", "")):
            data = twd._get_burndown_data()
        assert 0.0 <= data["projected_remaining_at_reset"] <= 100.0


class TestEmptyLedgerSeeding:
    def setup_method(self):
        _clear_burndown_cache()

    def test_empty_account_ledger_seeds_from_current_remaining(self):
        """Right after /switch-account the new account has no tagged ledger entries.

        The old behavior left raw_points empty → actual defaulted to [(0, 100)]
        → flat 100% chart → next refresh flipped to real value. The fix seeds
        a synthetic point at mins_elapsed with the live remaining_pct.
        """
        with _mock_current_pct(65), \
             patch.object(twd, "_load_ledger", return_value=[]), \
             patch.object(twd, "_get_active_account", return_value=("C", "", "")):
            data = twd._get_burndown_data()
        assert data["actual"], "actual should not be empty after empty-ledger seeding"
        # The seed point's remaining_pct should match live remaining (35)
        seed_remaining = data["actual"][-1][1]
        assert 30 <= seed_remaining <= 40, (
            f"seed point remaining={seed_remaining} should match live remaining_pct ~35"
        )
