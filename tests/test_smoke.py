"""Smoke tests for token_watch_data core query functions."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

import token_watch_data as twd
from tests.conftest import make_mock_urlopen


class TestGetDispatchQueue:
    def test_returns_valid_structure(self, sample_dispatch_tasks):
        """_get_dispatch_queue returns dict with queue, active, and stats keys."""
        mock_urlopen = make_mock_urlopen(sample_dispatch_tasks)

        with patch("urllib.request.urlopen", mock_urlopen):
            result = twd._get_dispatch_queue_sync()

        assert isinstance(result, dict), "result must be a dict"
        assert "queue" in result, "missing 'queue' key"
        assert "active" in result, "missing 'active' key"
        assert "stats" in result, "missing 'stats' key"

        stats = result["stats"]
        assert "total_ready" in stats
        assert "total_active" in stats
        assert "total_tokens_k" in stats
        assert "by_project" in stats

        # Verify items are correctly split by status
        assert result["stats"]["total_ready"] == 1   # only the 'ready' task
        assert result["stats"]["total_active"] == 1  # only the 'in_progress' task
        assert isinstance(result["queue"], list)
        assert isinstance(result["active"], list)


class TestGetBuildLedger:
    def test_returns_valid_structure(self, sample_build_ledger_data):
        """_get_build_ledger returns dict with items, by_company, and stats keys."""
        mock_urlopen = make_mock_urlopen(sample_build_ledger_data)

        with patch("urllib.request.urlopen", mock_urlopen):
            result = twd._get_build_ledger()

        assert isinstance(result, dict), "result must be a dict"
        assert "items" in result, "missing 'items' key"
        assert "by_company" in result, "missing 'by_company' key"
        assert "stats" in result, "missing 'stats' key"

        stats = result["stats"]
        assert stats["total"] == 3
        assert stats["untested"] == 2   # 2 of 3 items are untested
        assert stats["decisions"] == 1  # 1 item_type == 'decision'
        assert stats["sessions"] == 2   # cc-12345 and cc-99999
        assert stats["projects"] == 2   # token-watch and atlas-portal

        # by_company grouping
        assert "delphi" in result["by_company"]

    def test_returns_empty_on_network_error(self):
        """_get_build_ledger returns empty structure on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("network error")):
            result = twd._get_build_ledger()

        assert result["items"] == []
        assert result["by_company"] == {}
        assert result["stats"]["total"] == 0


class TestGetWindowScores:
    def test_returns_valid_structure(self, tmp_path):
        """_get_window_scores returns list of score dicts from JSONL file."""
        scores_file = tmp_path / "window-scores.jsonl"
        scores = [
            {"window": "2026-04-09T10:00:00", "overall": 4.5, "tokens_k": 120},
            {"window": "2026-04-09T11:00:00", "overall": 3.8, "tokens_k": 95},
            {"window": "2026-04-09T12:00:00", "overall": 5.0, "tokens_k": 200},
        ]
        scores_file.write_text("\n".join(json.dumps(s) for s in scores) + "\n")

        original = twd.WINDOW_SCORES_FILE
        try:
            twd.WINDOW_SCORES_FILE = scores_file
            result = twd._get_window_scores()
        finally:
            twd.WINDOW_SCORES_FILE = original

        assert isinstance(result, list), "result must be a list"
        assert len(result) == 3
        # Results are reversed (most recent first)
        assert result[0]["window"] == "2026-04-09T12:00:00"
        assert result[-1]["window"] == "2026-04-09T10:00:00"
        # Each entry has expected keys
        for entry in result:
            assert "overall" in entry
            assert "tokens_k" in entry

    def test_returns_empty_list_when_no_file(self, tmp_path):
        """_get_window_scores returns [] when the scores file doesn't exist."""
        original = twd.WINDOW_SCORES_FILE
        try:
            twd.WINDOW_SCORES_FILE = tmp_path / "nonexistent.jsonl"
            result = twd._get_window_scores()
        finally:
            twd.WINDOW_SCORES_FILE = original

        assert result == []

    def test_respects_limit(self, tmp_path):
        """_get_window_scores respects the limit parameter."""
        scores_file = tmp_path / "window-scores.jsonl"
        scores = [{"window": f"2026-04-09T{i:02d}:00:00", "overall": 4.0, "tokens_k": 50} for i in range(10)]
        scores_file.write_text("\n".join(json.dumps(s) for s in scores) + "\n")

        original = twd.WINDOW_SCORES_FILE
        try:
            twd.WINDOW_SCORES_FILE = scores_file
            result = twd._get_window_scores(limit=3)
        finally:
            twd.WINDOW_SCORES_FILE = original

        assert len(result) == 3
