"""Fleet monitor — observability for secondary CLI engines.

Gathers live activity of MM continuous loop, harvest job, Kimi/Codex
worktrees, Gem calls, and the Forge tmux panes. Pure functions, no UI.

Reads from:
- /tmp/forges/continuous-mm.log        (MM ticks)
- /tmp/forges/{research,audit,ops,bugs}/mm/   (MM per-role output)
- /tmp/forges/harvest/harvest.log      (harvest job)
- /tmp/forges/harvest/summaries/       (harvest progress)
- /tmp/forges/harvest/raw-heads/       (harvest total)
- /tmp/worktrees/                      (Kimi + Codex worktrees)
- tmux forges session                  (Forge panes)
- /tmp/forges/*.md                     (Gem recent output)

Entry point: collect_fleet_state() -> dict
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


FORGES_DIR = "/tmp/forges"
WORKTREES_DIR = "/tmp/worktrees"
MM_ROLES = ("research", "audit", "ops", "bugs")
ACTIVE_THRESHOLD_S = 60          # green dot
RECENT_THRESHOLD_S = 5 * 60      # yellow dot
# anything older -> gray (idle / dormant)


# ---------------------------------------------------------------------------
# Low-level helpers (overridable in tests)
# ---------------------------------------------------------------------------

def _run(cmd: List[str], timeout: float = 2.0) -> str:
    """Run a subprocess and return stdout (empty string on failure)."""
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return out.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _pgrep(pattern: str) -> Optional[int]:
    """Return PID (first match) for processes matching pattern, or None."""
    out = _run(["pgrep", "-f", pattern])
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def _mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _newest_mtime(paths: List[str]) -> Optional[float]:
    best = None
    for p in paths:
        m = _mtime(p)
        if m is not None and (best is None or m > best):
            best = m
    return best


def _status_from_age(age_s: Optional[float]) -> str:
    if age_s is None:
        return "idle"
    if age_s < ACTIVE_THRESHOLD_S:
        return "active"
    if age_s < RECENT_THRESHOLD_S:
        return "recent"
    return "idle"


def _fmt_age(age_s: Optional[float]) -> str:
    if age_s is None:
        return "—"
    a = int(age_s)
    if a < 60:
        return f"{a}s ago"
    if a < 3600:
        return f"{a // 60}m ago"
    return f"{a // 3600}h ago"


# ---------------------------------------------------------------------------
# MM (continuous-mm.sh)
# ---------------------------------------------------------------------------

_MM_TICK_RE = re.compile(r"\[tick (\d+) @ (\d{2}:\d{2}:\d{2})\] firing MM dump")


def collect_mm(now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    pid = _pgrep("continuous-mm.sh")
    log_path = f"{FORGES_DIR}/continuous-mm.log"
    log_mtime = _mtime(log_path)

    last_tick: Optional[int] = None
    last_tick_time: Optional[str] = None
    try:
        with open(log_path, "r") as f:
            # Read last 4KB only — we just need recent ticks.
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read()
        for m in _MM_TICK_RE.finditer(tail):
            last_tick = int(m.group(1))
            last_tick_time = m.group(2)
    except OSError:
        pass

    roles: Dict[str, Dict[str, Any]] = {}
    for role in MM_ROLES:
        role_dir = f"{FORGES_DIR}/{role}/mm"
        files: List[str] = []
        try:
            files = [f for f in os.listdir(role_dir) if not f.startswith(".")]
        except OSError:
            files = []
        latest_path: Optional[str] = None
        latest_age: Optional[float] = None
        if files:
            paths = [f"{role_dir}/{f}" for f in files]
            latest_path = max(paths, key=lambda p: _mtime(p) or 0)
            m = _mtime(latest_path)
            if m is not None:
                latest_age = now - m
        roles[role] = {
            "file_count": len(files),
            "latest_slug": _slug_from_filename(latest_path) if latest_path else None,
            "latest_age_s": latest_age,
        }

    age = (now - log_mtime) if log_mtime else None
    status = "active" if pid else _status_from_age(age)
    total_files = sum(r["file_count"] for r in roles.values())

    if pid:
        detail_bits = []
        if last_tick is not None:
            detail_bits.append(f"tick {last_tick}")
        detail_bits.append(f"{total_files} files")
        detail_bits.append(f"{len(MM_ROLES)} roles")
        detail = " · ".join(detail_bits)
    else:
        detail = "not running"

    return {
        "engine": "MM",
        "pid": pid,
        "status": status,
        "detail": detail,
        "last_age_s": age,
        "last_age": _fmt_age(age),
        "last_tick": last_tick,
        "last_tick_time": last_tick_time,
        "total_files": total_files,
        "roles": roles,
    }


def _slug_from_filename(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    name = os.path.basename(path)
    # Strip extension and any leading "t1-HHMMSS-" prefix.
    stem = re.sub(r"\.[^.]+$", "", name)
    stem = re.sub(r"^t\d+-\d{6}-", "", stem)
    return stem or None


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def collect_harvest(now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    pid = _pgrep("harvest-sessions.sh")
    log_path = f"{FORGES_DIR}/harvest/harvest.log"
    log_mtime = _mtime(log_path)

    def _count(d: str) -> int:
        try:
            return sum(1 for f in os.listdir(d) if not f.startswith("."))
        except OSError:
            return 0

    summarized = _count(f"{FORGES_DIR}/harvest/summaries")
    total = _count(f"{FORGES_DIR}/harvest/raw-heads")
    age = (now - log_mtime) if log_mtime else None
    status = "active" if pid else _status_from_age(age)

    if total:
        detail = f"{summarized}/{total} summarized"
    elif pid:
        detail = "running · awaiting input"
    else:
        detail = "not running"

    return {
        "engine": "HARVEST",
        "pid": pid,
        "status": status,
        "detail": detail,
        "summarized": summarized,
        "total": total,
        "last_age_s": age,
        "last_age": _fmt_age(age),
    }


# ---------------------------------------------------------------------------
# Worktrees (Kimi + Codex)
# ---------------------------------------------------------------------------

def _newest_file_age(root: str, now: float) -> Optional[float]:
    """Fastest approximation: newest file mtime anywhere under root."""
    best: Optional[float] = None
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # skip .git internals for speed
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                m = _mtime(os.path.join(dirpath, fn))
                if m is not None and (best is None or m > best):
                    best = m
    except OSError:
        return None
    if best is None:
        return None
    return now - best


def collect_worktrees(now: Optional[float] = None, deep_scan: bool = False) -> Dict[str, Any]:
    """Inspect /tmp/worktrees/ for Kimi + Codex activity.

    deep_scan=False (default) uses only the worktree dir's own mtime for speed.
    deep_scan=True walks files to find the true newest mtime.
    """
    now = now if now is not None else time.time()
    buckets = {
        "kimi": {"total": 0, "active": 0, "newest_age_s": None, "newest_name": None},
        "codex": {"total": 0, "active": 0, "newest_age_s": None, "newest_name": None},
    }

    try:
        entries = os.listdir(WORKTREES_DIR)
    except OSError:
        entries = []

    for name in entries:
        full = os.path.join(WORKTREES_DIR, name)
        if not os.path.isdir(full):
            continue
        if name.startswith("kimi-"):
            key = "kimi"
        elif name.startswith("codex-"):
            key = "codex"
        else:
            continue
        buckets[key]["total"] += 1

        if deep_scan:
            age = _newest_file_age(full, now)
        else:
            m = _mtime(full)
            age = (now - m) if m is not None else None

        if age is not None and age < RECENT_THRESHOLD_S:
            buckets[key]["active"] += 1
        cur = buckets[key]["newest_age_s"]
        if age is not None and (cur is None or age < cur):
            buckets[key]["newest_age_s"] = age
            buckets[key]["newest_name"] = name

    result = {}
    for key, b in buckets.items():
        age = b["newest_age_s"]
        status = _status_from_age(age)
        if b["total"] == 0:
            detail = "no worktrees"
        elif b["active"]:
            detail = f"{b['active']}/{b['total']} worktrees building"
        else:
            detail = f"{b['total']} worktrees (no recent writes)"
        result[key] = {
            "engine": key.upper(),
            "status": status,
            "detail": detail,
            "total": b["total"],
            "active": b["active"],
            "newest_name": b["newest_name"],
            "last_age_s": age,
            "last_age": _fmt_age(age),
        }
    return result


# ---------------------------------------------------------------------------
# Gem (Gemini) — approximated by recent .md files in /tmp/forges/
# ---------------------------------------------------------------------------

def collect_gem(now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    candidates: List[Tuple[str, float]] = []
    try:
        for name in os.listdir(FORGES_DIR):
            if not name.endswith(".md"):
                continue
            full = os.path.join(FORGES_DIR, name)
            m = _mtime(full)
            if m is not None:
                candidates.append((full, m))
    except OSError:
        pass
    # Also check harvest/final-synopsis.md
    synopsis = f"{FORGES_DIR}/harvest/final-synopsis.md"
    m = _mtime(synopsis)
    if m is not None:
        candidates.append((synopsis, m))

    if not candidates:
        return {
            "engine": "GEM",
            "status": "idle",
            "detail": "no gem output found",
            "last_age_s": None,
            "last_age": "—",
            "latest_file": None,
        }

    latest_path, latest_mtime = max(candidates, key=lambda x: x[1])
    age = now - latest_mtime
    status = _status_from_age(age)
    detail = f"last output: {os.path.basename(latest_path)}"
    return {
        "engine": "GEM",
        "status": status,
        "detail": detail,
        "last_age_s": age,
        "last_age": _fmt_age(age),
        "latest_file": latest_path,
    }


# ---------------------------------------------------------------------------
# Forges tmux panes
# ---------------------------------------------------------------------------

def collect_forges(now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else time.time()
    # List panes
    panes_out = _run([
        "tmux", "list-panes", "-t", "forges",
        "-F", "#{pane_index} #{pane_current_command}",
    ])
    panes: List[Dict[str, Any]] = []
    if not panes_out.strip():
        return {
            "engine": "FORGES",
            "status": "idle",
            "detail": "tmux session 'forges' not found",
            "panes": [],
        }

    for line in panes_out.splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        idx = parts[0]
        cmd = parts[1] if len(parts) > 1 else ""
        last_line = _capture_pane_last_line(idx)
        panes.append({
            "index": idx,
            "command": cmd,
            "last_line": last_line,
        })

    # Heuristic: if any pane's tail line contains activity markers the fleet
    # is active; if all show an accept-edits / idle prompt, it's idle.
    idle_markers = ("accept edits", "1. Yes", "waiting", "idle")
    active_count = 0
    for p in panes:
        ll = (p.get("last_line") or "").lower()
        if not ll:
            continue
        if any(m.lower() in ll for m in idle_markers):
            continue
        active_count += 1

    if not panes:
        status = "idle"
        detail = "no panes"
    elif active_count == 0:
        status = "idle"
        detail = f'all {len(panes)} panes on "accept edits" prompt'
    else:
        status = "active" if active_count >= len(panes) / 2 else "recent"
        detail = f"{active_count}/{len(panes)} panes active"

    return {
        "engine": "FORGES",
        "status": status,
        "detail": detail,
        "panes": panes,
    }


def _capture_pane_last_line(pane_index: str) -> str:
    out = _run([
        "tmux", "capture-pane", "-t", f"forges:0.{pane_index}", "-p",
    ])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _capture_pane_last_lines(pane_index: str, n: int = 2) -> List[str]:
    out = _run([
        "tmux", "capture-pane", "-t", f"forges:0.{pane_index}", "-p",
    ])
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-n:] if len(lines) >= n else lines


# ---------------------------------------------------------------------------
# Forge Matrix
# ---------------------------------------------------------------------------

FORGE_ROLE_ORDER = ["forge-prime", "forge-research", "forge-audit", "forge-ops", "forge-bugs"]
FORGE_PANE_ROLES = ["forge-research", "forge-audit", "forge-ops", "forge-bugs"]


def _get_forge_session_locks() -> List[Dict[str, Any]]:
    """Fetch active forge sessions from Supabase session_locks."""
    try:
        from token_watch_data import _SUPABASE_URL, __SUPABASE_KEY
    except Exception:
        return []

    import urllib.request
    import json as _json

    url = (
        f"{_SUPABASE_URL}/session_locks"
        "?status=eq.active"
        "&role=like.*forge*"
        "&order=role.asc"
        "&select=role,session_id,task_name,heartbeat_at,claimed_at,model"
    )
    req = urllib.request.Request(url, headers={
        "apikey": __SUPABASE_KEY,
        "Authorization": "Bearer " + __SUPABASE_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception:
        return []


def _heartbeat_age(heartbeat_at: Optional[str], now: float) -> Optional[float]:
    if not heartbeat_at:
        return None
    try:
        from datetime import datetime, timezone
        # Handle ISO format with or without Z
        ts = heartbeat_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        return now - dt.timestamp()
    except Exception:
        return None


def collect_forge_matrix(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Return rows for the Forge Matrix.

    Each row: role, session_id, task_name, heartbeat_age, last_lines.
    Prime row shows '(this session)' for tmux lines.
    """
    now = now if now is not None else time.time()
    locks = _get_forge_session_locks()
    lock_by_role = {row["role"]: row for row in locks if row.get("role")}

    # Map tmux panes 0-3 to forge roles (research, audit, ops, bugs)
    pane_lines: Dict[str, List[str]] = {}
    for i, role in enumerate(FORGE_PANE_ROLES):
        pane_lines[role] = _capture_pane_last_lines(str(i), n=2)

    rows = []
    for role in FORGE_ROLE_ORDER:
        lock = lock_by_role.get(role, {})
        hb_age = _heartbeat_age(lock.get("heartbeat_at"), now)
        if role == "forge-prime":
            lines = ["(this session)"]
        else:
            lines = pane_lines.get(role, [])
        rows.append({
            "role": role,
            "session_id": lock.get("session_id", "—"),
            "task_name": lock.get("task_name", "—"),
            "heartbeat_age_s": hb_age,
            "heartbeat_age": _fmt_age(hb_age),
            "last_lines": lines,
            "model": lock.get("model", ""),
        })
    return rows


# ---------------------------------------------------------------------------
# Token Matrix — log scanning helpers
# ---------------------------------------------------------------------------

_LOG_ENGINE_PATTERNS = {
    "gemini": re.compile(r"\bgemini\b", re.IGNORECASE),
    "grok": re.compile(r"\bgrok\b", re.IGNORECASE),
    "kimi": re.compile(r"\bkimi\b", re.IGNORECASE),
    "mm": re.compile(r"\bmm\b|\bminimax\b", re.IGNORECASE),
    "opus": re.compile(r"\bopus\b", re.IGNORECASE),
}


def _scan_log_engines(now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Scan /tmp/forges/**/*.log for most recent mention of each engine.

    Returns a dict mapping engine -> {age_s, age, line, path}.
    """
    now = now if now is not None else time.time()
    best: Dict[str, Tuple[float, str, str]] = {}

    log_paths: List[str] = []
    for root, _dirs, files in os.walk(FORGES_DIR):
        for f in files:
            if f.endswith(".log"):
                log_paths.append(os.path.join(root, f))

    for path in log_paths:
        m = _mtime(path)
        if m is None:
            continue
        file_age = now - m
        # Skip logs older than 1 hour for engine scanning
        if file_age > 3600:
            continue
        try:
            with open(path, "r", errors="ignore") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                tail = fh.read()
        except OSError:
            continue

        for engine, pattern in _LOG_ENGINE_PATTERNS.items():
            for line in reversed(tail.splitlines()):
                if pattern.search(line):
                    # Use file mtime as proxy for line age; keep newest file per engine
                    if engine not in best or file_age < best[engine][0]:
                        best[engine] = (file_age, line.strip(), path)
                    break

    result: Dict[str, Dict[str, Any]] = {}
    for engine in _LOG_ENGINE_PATTERNS:
        if engine in best:
            age_s, line, path = best[engine]
            result[engine] = {
                "age_s": age_s,
                "age": _fmt_age(age_s),
                "line": line,
                "path": path,
            }
        else:
            result[engine] = {
                "age_s": None,
                "age": "—",
                "line": "",
                "path": "",
            }
    return result


# ---------------------------------------------------------------------------
# Token Matrix
# ---------------------------------------------------------------------------

def collect_token_matrix(now: Optional[float] = None) -> Dict[str, Any]:
    """Return thinking and working sandwich data.

    Thinking: Opus(plan) -> Gemini+Grok -> Opus(synthesize)
    Working:  Opus(design) -> Kimi -> MM -> Kimi -> Opus(audit)
    """
    now = now if now is not None else time.time()
    fleet = collect_fleet_state(deep_worktree_scan=False)
    logs = _scan_log_engines(now=now)
    forge_locks = _get_forge_session_locks()
    lock_by_role = {row["role"]: row for row in forge_locks if row.get("role")}

    def _forge_slice(role: str, default_task: str = "—") -> Dict[str, Any]:
        lock = lock_by_role.get(role, {})
        hb_age = _heartbeat_age(lock.get("heartbeat_at"), now)
        return {
            "engine": "Opus",
            "task": lock.get("task_name") or default_task,
            "age_s": hb_age,
            "age": _fmt_age(hb_age),
            "status": _status_from_age(hb_age),
            "session_id": lock.get("session_id", "—"),
        }

    gem = fleet["gem"]
    gem_slice = {
        "engine": "Gemini",
        "task": gem.get("detail", "—"),
        "age_s": gem.get("last_age_s"),
        "age": gem.get("last_age", "—"),
        "status": gem.get("status", "idle"),
    }

    grok = logs.get("grok", {})
    grok_slice = {
        "engine": "Grok",
        "task": grok.get("line", "no recent activity")[:40] or "no recent activity",
        "age_s": grok.get("age_s"),
        "age": grok.get("age", "—"),
        "status": _status_from_age(grok.get("age_s")),
    }

    mm = fleet["mm"]
    mm_slice = {
        "engine": "MiniMax",
        "task": mm.get("detail", "—"),
        "age_s": mm.get("last_age_s"),
        "age": mm.get("last_age", "—"),
        "status": mm.get("status", "idle"),
    }

    kimi = fleet["kimi"]
    kimi_slice = {
        "engine": "Kimi",
        "task": kimi.get("detail", "—"),
        "age_s": kimi.get("last_age_s"),
        "age": kimi.get("last_age", "—"),
        "status": kimi.get("status", "idle"),
    }

    codex = fleet["codex"]
    codex_slice = {
        "engine": "Codex",
        "task": codex.get("detail", "—"),
        "age_s": codex.get("last_age_s"),
        "age": codex.get("last_age", "—"),
        "status": codex.get("status", "idle"),
    }

    return {
        "thinking": {
            "top": _forge_slice("forge-research", "Forge-Research plan"),
            "meat": [gem_slice, grok_slice],
            "bottom": _forge_slice("forge-audit", "Forge-Audit synthesis"),
        },
        "working": {
            "top": _forge_slice("forge-research", "Forge-Research design"),
            "meat": [kimi_slice, mm_slice, codex_slice],
            "bottom": _forge_slice("forge-audit", "Forge-Audit audit"),
        },
    }


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def collect_fleet_state(deep_worktree_scan: bool = False) -> Dict[str, Any]:
    """Gather the full fleet snapshot.

    Returns a dict with keys:
      - collected_at: float (epoch seconds)
      - mm:       dict from collect_mm()
      - harvest:  dict from collect_harvest()
      - kimi:     dict (from collect_worktrees)
      - codex:    dict (from collect_worktrees)
      - gem:      dict from collect_gem()
      - forges:   dict from collect_forges()

    All engine dicts share at minimum: engine, status, detail, last_age.
    status is one of: active / recent / idle.
    """
    now = time.time()
    wt = collect_worktrees(now=now, deep_scan=deep_worktree_scan)
    return {
        "collected_at": now,
        "mm": collect_mm(now=now),
        "harvest": collect_harvest(now=now),
        "kimi": wt["kimi"],
        "codex": wt["codex"],
        "gem": collect_gem(now=now),
        "forges": collect_forges(now=now),
    }


if __name__ == "__main__":  # pragma: no cover
    import json
    print(json.dumps(collect_fleet_state(), indent=2, default=str))
