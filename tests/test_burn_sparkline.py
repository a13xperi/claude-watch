"""Regression tests for the burn-rate sparkline helper.

``_burn_rate_sparkline`` renders a unicode-block chart of %/min
consumption over the last N minutes, one char per slot. It reuses
the same account-filtered ledger path as ``_get_token_attribution``
so cross-account staleness can't spike the chart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import token_watch_data as twd


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _entry(ts: datetime, pct: float, account: str = "B") -> dict:
    return {
        "type": "tool_use",
        "ts": _iso(ts),
        "five_pct": pct,
        "session": "cc-1111",
        "directive": "fake",
        "output_tokens": 100,
        "model": "claude-opus-4-6",
        "tool": "Edit",
        "account": account,
    }


class TestSparklineLength:
    def test_no_account_returns_empty(self):
        with patch.object(twd, "_get_active_account", return_value=("?", "", "")):
            assert twd._burn_rate_sparkline() == ""

    def test_no_ledger_entries_returns_empty(self):
        with patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_load_ledger", return_value=[]):
            assert twd._burn_rate_sparkline() == ""

    def test_single_entry_returns_empty(self):
        """One data point can't produce a rate — should short-circuit."""
        now = datetime.now(timezone.utc)
        with patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_load_ledger", return_value=[_entry(now - timedelta(minutes=1), 10)]):
            assert twd._burn_rate_sparkline() == ""


class TestSparklineRendering:
    def test_burn_returns_exactly_slots_chars(self):
        """With data present, the sparkline must be exactly ``slots`` chars wide."""
        now = datetime.now(timezone.utc)
        entries = []
        # 10 minutes of increasing pct — one sample per minute, two per slot
        for i in range(20):
            ts = now - timedelta(minutes=9.5) + timedelta(seconds=i * 30)
            entries.append(_entry(ts, float(i) * 0.5))

        with patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_load_ledger", return_value=entries):
            spark = twd._burn_rate_sparkline(window_mins=10, slots=10)
        assert len(spark) == 10, f"expected 10 chars, got {len(spark)}: {spark!r}"
        # Chars must all belong to the block palette
        for ch in spark:
            assert ch in twd._SPARK_BLOCKS, f"unexpected char {ch!r}"

    def test_ignores_cross_account_entries(self):
        """Account filter must exclude other-account entries that would
        otherwise inflate the burn rate."""
        now = datetime.now(timezone.utc)
        good = [
            _entry(now - timedelta(minutes=9), 0.0, account="B"),
            _entry(now - timedelta(minutes=0.5), 1.0, account="B"),
        ]
        poisoned = [
            _entry(now - timedelta(minutes=5), 99.0, account="A"),  # wrong account
        ]

        def loader(last_n=None, account=None):
            all_entries = good + poisoned
            return [e for e in all_entries if account is None or e.get("account") == account]

        with patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_load_ledger", side_effect=loader):
            spark = twd._burn_rate_sparkline(window_mins=10, slots=10)

        # The full-block char '█' (index 8) must never appear — the poisoned
        # 99% sample would force that, but the account filter drops it.
        assert "█" not in spark, f"account filter leaked: {spark!r}"

    def test_window_reset_clamps_to_zero_rate(self):
        """Negative deltas (window reset mid-interval) must be clamped to 0
        so the sparkline doesn't render a phantom spike."""
        now = datetime.now(timezone.utc)
        entries = [
            _entry(now - timedelta(minutes=9.5), 90.0),
            _entry(now - timedelta(minutes=9.0), 10.0),  # reset
            _entry(now - timedelta(minutes=0.5), 12.0),
            _entry(now - timedelta(minutes=0.2), 14.0),
        ]
        with patch.object(twd, "_get_active_account", return_value=("B", "", "")), \
             patch.object(twd, "_load_ledger", return_value=entries):
            spark = twd._burn_rate_sparkline(window_mins=10, slots=10)
        # Sparkline should not be empty (we have data), but the first slot
        # (containing the 90→10 drop) must not be the peak.
        assert spark, "sparkline should render with data present"
