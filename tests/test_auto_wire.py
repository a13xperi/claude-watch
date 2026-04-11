"""Regression tests for the auto-wire chain (task #13238).

The "real-time push" flow lives in battlestation hooks and lib:

    hooks/auto-register.sh      — registers the session in session_locks
    hooks/wire-inbox.sh         — polls session_messages every 30s per tool call
    hooks/file-lock-check.sh    — blocks Edit/Write and auto-sends file_release
    lib/wire.sh                 — wire_send, wire_request_file_release, etc.

This test asserts the invariants of that chain so future refactors can't
silently break it. The file-lock hook supports a ``PEERS_FILE`` env
override specifically so we can feed it a fixture without touching the
live ``/tmp/claude-peers.json`` shared with other sessions.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


BATTLESTATION = Path.home() / "battlestation"
HOOKS = BATTLESTATION / "hooks"
LIB = BATTLESTATION / "lib"

FILE_LOCK_HOOK = HOOKS / "file-lock-check.sh"
WIRE_INBOX_HOOK = HOOKS / "wire-inbox.sh"
AUTO_REGISTER_HOOK = HOOKS / "auto-register.sh"
WIRE_LIB = LIB / "wire.sh"


def _needs_battlestation():
    if not BATTLESTATION.exists():
        pytest.skip("battlestation repo not present on this host")


# ---------------------------------------------------------------------------
# Static invariants — the files and functions the chain depends on must exist.
# ---------------------------------------------------------------------------


class TestAutoWireChainInvariants:
    def test_file_lock_hook_exists_and_executable(self):
        _needs_battlestation()
        assert FILE_LOCK_HOOK.exists(), "hooks/file-lock-check.sh missing"
        mode = FILE_LOCK_HOOK.stat().st_mode
        assert mode & stat.S_IXUSR, "file-lock-check.sh is not executable"

    def test_wire_inbox_hook_exists(self):
        _needs_battlestation()
        assert WIRE_INBOX_HOOK.exists(), "hooks/wire-inbox.sh missing"

    def test_auto_register_hook_exists(self):
        _needs_battlestation()
        assert AUTO_REGISTER_HOOK.exists(), "hooks/auto-register.sh missing"

    def test_wire_lib_exists(self):
        _needs_battlestation()
        assert WIRE_LIB.exists(), "lib/wire.sh missing"

    def test_wire_request_file_release_defined(self):
        """lib/wire.sh must declare wire_request_file_release and the msg_ alias."""
        _needs_battlestation()
        src = WIRE_LIB.read_text()
        assert "wire_request_file_release()" in src, (
            "wire_request_file_release function missing from lib/wire.sh"
        )
        assert "msg_request_file_release()" in src, (
            "msg_request_file_release back-compat alias missing"
        )

    def test_file_lock_hook_calls_msg_request_file_release(self):
        """The hook must invoke msg_request_file_release when it blocks an edit."""
        _needs_battlestation()
        src = FILE_LOCK_HOOK.read_text()
        assert "msg_request_file_release" in src, (
            "file-lock-check.sh no longer calls msg_request_file_release — "
            "auto-wire chain is broken"
        )


# ---------------------------------------------------------------------------
# Behavioural tests — run the hook end-to-end with a fixture peers file.
# ---------------------------------------------------------------------------


def _write_peers(tmp: Path, peers: list) -> Path:
    path = tmp / "claude-peers.json"
    path.write_text(json.dumps(peers))
    return path


def _run_hook(hook: Path, tool_input: dict, peers_file: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PEERS_FILE"] = str(peers_file)
    return subprocess.run(
        ["bash", str(hook)],
        input=json.dumps(tool_input),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _init_repo(tmp: Path) -> Path:
    """Init a throwaway git repo so the hook's ``git rev-parse`` call resolves.

    Without a real git root, ``REL_PATH`` collapses to the absolute path and
    won't match a basename stored in ``files_touched`` — which exactly mirrors
    production behaviour but makes the behavioural tests impossible to write.
    """
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    return repo


class TestFileLockHookBehaviour:
    def test_blocks_when_peer_owns_file(self, tmp_path):
        """Given a live peer claiming our target file, the hook must block (exit 2)."""
        _needs_battlestation()

        repo = _init_repo(tmp_path)
        target = repo / "sample.py"
        target.write_text("print('hello')\n")

        peers = [
            {
                "session_id": "cc-99999",
                "task_name": "peer is editing sample.py",
                "repo": "token-watch",
                "heartbeat_at": _iso(datetime.now(timezone.utc)),
                "files_touched": ["sample.py"],
            }
        ]
        peers_file = _write_peers(tmp_path, peers)

        result = _run_hook(
            FILE_LOCK_HOOK,
            {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
            peers_file,
        )

        assert result.returncode == 2, (
            f"expected exit 2 (blocked), got {result.returncode}. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "BLOCKED" in result.stdout
        assert "cc-99999" in result.stdout

    def test_allows_when_only_stale_peer_claims_file(self, tmp_path):
        """A peer whose heartbeat is older than 5 minutes must be ignored."""
        _needs_battlestation()

        repo = _init_repo(tmp_path)
        target = repo / "sample.py"
        target.write_text("x = 1\n")

        peers = [
            {
                "session_id": "cc-88888",
                "task_name": "zombie session",
                "repo": "token-watch",
                "heartbeat_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=30)),
                "files_touched": ["sample.py"],
            }
        ]
        peers_file = _write_peers(tmp_path, peers)

        result = _run_hook(
            FILE_LOCK_HOOK,
            {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
            peers_file,
        )

        assert result.returncode == 0, (
            f"expected exit 0 (allowed through stale peer), got {result.returncode}. "
            f"stdout={result.stdout!r}"
        )
        assert "BLOCKED" not in result.stdout

    def test_allows_when_peers_file_empty(self, tmp_path):
        """No peers → no conflict → pass-through."""
        _needs_battlestation()

        repo = _init_repo(tmp_path)
        target = repo / "sample.py"
        target.write_text("y = 2\n")
        peers_file = _write_peers(tmp_path, [])

        result = _run_hook(
            FILE_LOCK_HOOK,
            {"tool_name": "Write", "tool_input": {"file_path": str(target)}},
            peers_file,
        )

        assert result.returncode == 0

    def test_skips_non_source_files(self, tmp_path):
        """Markdown, JSON, .env and similar are skipped — always exit 0 early."""
        _needs_battlestation()

        repo = _init_repo(tmp_path)
        target = repo / "README.md"
        target.write_text("# hi")
        peers = [
            {
                "session_id": "cc-77777",
                "task_name": "peer owns README",
                "repo": "token-watch",
                "heartbeat_at": _iso(datetime.now(timezone.utc)),
                "files_touched": ["README.md"],
            }
        ]
        peers_file = _write_peers(tmp_path, peers)

        result = _run_hook(
            FILE_LOCK_HOOK,
            {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
            peers_file,
        )

        assert result.returncode == 0, "markdown edit should not be blocked"

    def test_ignores_non_edit_tools(self, tmp_path):
        """Read/Grep/etc. must not trigger the lock — only Edit/Write."""
        _needs_battlestation()

        repo = _init_repo(tmp_path)
        target = repo / "sample.py"
        target.write_text("z = 3\n")
        peers = [
            {
                "session_id": "cc-66666",
                "task_name": "peer owns sample.py",
                "repo": "token-watch",
                "heartbeat_at": _iso(datetime.now(timezone.utc)),
                "files_touched": ["sample.py"],
            }
        ]
        peers_file = _write_peers(tmp_path, peers)

        result = _run_hook(
            FILE_LOCK_HOOK,
            {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
            peers_file,
        )

        assert result.returncode == 0, "Read tool must bypass file-lock check"

    def test_allows_own_session_claim(self, tmp_path):
        """If the peer claiming the file IS us, the hook must not block itself.

        The hook derives ``MY_ID`` from ``$PPID`` at runtime. Because we spawn
        the hook via ``subprocess.run``, its ``$PPID`` equals the parent
        ``bash`` PID — which differs from ours. We probe that PID first, then
        write the peers fixture with ``session_id = cc-${that_ppid}`` so the
        self-match code path (``if s['session_id'] == my_id: continue``) fires.
        """
        _needs_battlestation()

        repo = _init_repo(tmp_path)
        target = repo / "sample.py"
        target.write_text("w = 4\n")

        env = os.environ.copy()
        env["PEERS_FILE"] = str(tmp_path / "claude-peers.json")

        probe = subprocess.run(
            ["bash", "-c", "echo $PPID"],
            capture_output=True,
            text=True,
            env=env,
        )
        hook_ppid = probe.stdout.strip()
        assert hook_ppid, "failed to probe subshell PPID"

        peers = [
            {
                "session_id": f"cc-{hook_ppid}",
                "task_name": "this is me",
                "repo": "token-watch",
                "heartbeat_at": _iso(datetime.now(timezone.utc)),
                "files_touched": ["sample.py"],
            }
        ]
        _write_peers(tmp_path, peers)

        result = subprocess.run(
            ["bash", str(FILE_LOCK_HOOK)],
            input=json.dumps(
                {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
            ),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

        assert result.returncode == 0, (
            f"hook must not block when the claiming peer IS us. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
