"""Shared fixtures for token-watch tests."""
import json
import io
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_session_data():
    return [
        {
            "session_id": "cc-12345",
            "tool": "claude-code",
            "repo": "token-watch",
            "status": "active",
            "account": "A",
            "task_name": "Test task",
            "files_touched": [],
        },
        {
            "session_id": "cc-67890",
            "tool": "claude-code",
            "repo": "atlas-portal",
            "status": "done",
            "account": "B",
            "task_name": "Another task",
            "files_touched": ["src/app.tsx"],
        },
    ]


@pytest.fixture
def sample_build_ledger_data():
    return [
        {
            "id": 1,
            "session_id": "cc-12345",
            "project": "token-watch",
            "company": "delphi",
            "item_type": "feature",
            "title": "Add dispatch tab",
            "source": "commit",
            "test_status": "untested",
            "created_at": "2026-04-09T10:00:00Z",
        },
        {
            "id": 2,
            "session_id": "cc-12345",
            "project": "token-watch",
            "company": "delphi",
            "item_type": "decision",
            "title": "Wire uses Supabase not files",
            "source": "manual",
            "test_status": "untested",
            "created_at": "2026-04-09T11:00:00Z",
        },
        {
            "id": 3,
            "session_id": "cc-99999",
            "project": "atlas-portal",
            "company": "delphi",
            "item_type": "fix",
            "title": "Fix ESLint warning",
            "source": "commit",
            "test_status": "tested",
            "created_at": "2026-04-09T12:00:00Z",
        },
    ]


@pytest.fixture
def sample_dispatch_tasks():
    return [
        {
            "id": 211,
            "task_name": "Add test infrastructure",
            "dispatch_prompt": "Add pytest...",
            "project": "token-watch",
            "company": "delphi",
            "status": "ready",
            "tier": "auto",
            "priority": "medium",
            "difficulty": "medium",
            "points": 3,
            "est_tokens_k": 50,
            "source": "backlog",
            "claimed_by": None,
            "run_count": 0,
            "notes": None,
            "created_at": "2026-04-01T00:00:00Z",
            "build_order": 1,
            "lane": "infra",
        },
        {
            "id": 212,
            "task_name": "Replace bare except blocks",
            "dispatch_prompt": "Replace bare...",
            "project": "token-watch",
            "company": "delphi",
            "status": "in_progress",
            "tier": "auto",
            "priority": "medium",
            "difficulty": "low",
            "points": 2,
            "est_tokens_k": 30,
            "source": "backlog",
            "claimed_by": "cc-11111",
            "run_count": 1,
            "notes": None,
            "created_at": "2026-04-02T00:00:00Z",
            "build_order": 2,
            "lane": "infra",
        },
    ]


# ---------------------------------------------------------------------------
# Mock urllib helpers
# ---------------------------------------------------------------------------

def make_mock_urlopen(response_data):
    """Return a context-manager mock for urllib.request.urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen = MagicMock(return_value=mock_resp)
    return mock_urlopen
