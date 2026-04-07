"""
claude-watch data layer — shared by Rich and Textual versions.
All data fetching, caching, and computation lives here.
"""

import csv
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.panel import Panel
from rich.table import Table

(Path.home() / ".claude/logs").mkdir(parents=True, exist_ok=True)
_log = logging.getLogger("claude_watch")
_log_handler = logging.FileHandler(Path.home() / ".claude/logs/claude-watch.log")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_log.addHandler(_log_handler)
_log.setLevel(logging.WARNING)

# ── constants ────────────────────────────────────────────────────────────────

LEDGER = Path.home() / ".claude/logs/token-ledger.jsonl"
BUDGET_FILE = Path.home() / ".claude/token-budget.json"
TRANSCRIPTS_DIR = Path.home() / ".claude/projects/-Users-a13xperi"
ALL_PROJECT_DIRS = Path.home() / ".claude/projects"
SESSION_INDEX = Path.home() / ".claude/logs/session-index.jsonl"
PAPERCLIP_AGENTS_FILE = Path(__file__).parent / "paperclip_agents.json"

_PAPERCLIP_RE = re.compile(
    r"paperclip-instances-default-(?:projects|workspaces)-"
    r"([a-f0-9-]{36})-([a-f0-9-]{36})--default"
)
_PAPERCLIP_WS_RE = re.compile(
    r"paperclip-instances-default-workspaces-([a-f0-9-]{36})$"
)

_paperclip_map = {}   # type: Dict[str, Dict]
_paperclip_agents_flat = {}  # agent_uuid -> (company, name)


def _load_paperclip_map():
    global _paperclip_map, _paperclip_agents_flat
    try:
        data = json.loads(PAPERCLIP_AGENTS_FILE.read_text())
        _paperclip_map = data.get("projects", {})
        # Build flat agent UUID → (company, name) for workspace lookups
        for proj_info in _paperclip_map.values():
            company = proj_info.get("company", "?")
            for agent_uuid, name in proj_info.get("agents", {}).items():
                _paperclip_agents_flat[agent_uuid] = (company, name)
    except Exception as e:
        _log.warning("Failed to load paperclip map: %s", e)
        _paperclip_map = {}
        _paperclip_agents_flat = {}


_load_paperclip_map()


# ── helpers ──────────────────────────────────────────────────────────────────

def _current_pct():
    """Returns (five, seven, five_reset_ts, seven_reset_ts)."""
    try:
        r = subprocess.run(
            ["bash", "-c", "cat /tmp/statusline-debug.json 2>/dev/null"],
            capture_output=True, text=True, timeout=2,
        )
        if r.stdout.strip():
            d = json.loads(r.stdout)
            rl = d.get("rate_limits", {})
            five = rl.get("five_hour", {}).get("used_percentage", "?")
            seven = rl.get("seven_day", {}).get("used_percentage", "?")

            def _ts(raw):
                if isinstance(raw, (int, float)):
                    return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
                return raw or ""

            five_reset = _ts(rl.get("five_hour", {}).get("resets_at", ""))
            seven_reset = _ts(rl.get("seven_day", {}).get("resets_at", ""))
            return five, seven, five_reset, seven_reset
    except Exception as e:
        _log.warning("Failed to read rate limits: %s", e)
    return "?", "?", "", ""


def _countdown(reset_ts):
    if not reset_ts:
        return "?"
    try:
        reset = datetime.fromisoformat(reset_ts.replace("Z", "+00:00"))
        diff = int((reset - datetime.now(timezone.utc)).total_seconds())
        if diff <= 0:
            return "resetting..."
        h, rem = divmod(diff, 3600)
        m = rem // 60
        local_time = reset.astimezone().strftime("%-I:%M %p")
        return f"{h}h{m:02d}m (at {local_time})"
    except Exception:
        return "?"


def _reset_day(reset_ts):
    if not reset_ts:
        return "?"
    try:
        dt = datetime.fromisoformat(reset_ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime(f"%a %b {dt.day} %-I:%M %p")
    except Exception:
        return "?"


def _abbrev_model(model):
    if not model:
        return "?"
    m = model.lower()
    # Extract context tier if present (e.g. opus[1m] → opus:1m)
    tier = ""
    if "[" in m and "]" in m:
        tier = ":" + m[m.index("[") + 1:m.index("]")]
    if "opus" in m:
        return "opus" + tier
    if "sonnet" in m:
        return "sonnet" + tier
    if "haiku" in m:
        return "haiku" + tier
    return model[:10]


def _budget():
    try:
        if BUDGET_FILE.exists():
            return json.loads(BUDGET_FILE.read_text()).get("per_session_pct", 15)
    except Exception:
        pass
    return 15


def _active_sessions():
    """Return list of (pid, age_str, directive, delta) for active claude sessions."""
    sessions = []
    try:
        r = subprocess.run(
            ["ps", "ax", "-o", "pid,etime,command"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, etime, cmd = parts
            if cmd.strip() in ("claude", "/usr/local/bin/claude", "/opt/homebrew/bin/claude"):
                directive = ""
                try:
                    directive = Path(f"/tmp/claude-directive-{pid}").read_text().strip()
                except Exception:
                    pass
                delta = "?"
                try:
                    state_parts = Path(f"/tmp/claude-token-state-{pid}").read_text().split()
                    start_pct = float(state_parts[0])
                    start_epoch = float(state_parts[1]) if len(state_parts) > 1 else 0
                    cur = float(_current_pct()[0])
                    raw_delta = round(cur - start_pct, 1)
                    # Fix ghost session: if session just started and shows huge delta,
                    # it's measuring global drift, not actual consumption
                    age_secs = time.time() - start_epoch if start_epoch else 999
                    if raw_delta < 0:
                        delta = f"↻{cur:.0f}%"  # 5h window reset — show current absolute pct
                    elif age_secs < 120 and raw_delta > 5:
                        delta = "new"
                    else:
                        delta = f"+{raw_delta}%"
                except Exception:
                    pass
                source = _detect_source(pid)
                sessions.append((pid, etime, directive or "—", delta, source))
    except Exception:
        pass
    # Sort newest first (shortest etime = most recently spawned)
    sessions.sort(key=lambda s: _etime_to_secs(s[1]) or 0)
    return sessions


def _active_pids():
    """Return set of active cc-{PID} session IDs."""
    return {f"cc-{item[0]}" for item in _active_sessions()}


# ── peer sessions (Supabase session_locks) ────────────────────────────────

_peer_cache = None  # type: Optional[Tuple[float, List[Dict[str, Any]]]]
_PEER_CACHE_TTL = 10  # seconds


def _get_peer_sessions():
    # type: () -> List[Dict[str, Any]]
    """Fetch active sessions from Supabase session_locks table.

    Returns list of dicts with: session_id, tool, repo, task_name, account,
    claimed_at, heartbeat_at, files_touched.  Cached for 10 seconds.
    """
    global _peer_cache
    now = time.time()
    if _peer_cache is not None:
        cached_at, cached_data = _peer_cache
        if now - cached_at < _PEER_CACHE_TTL:
            return cached_data

    import urllib.request
    import json as _json

    url = (
        "{base}/session_locks"
        "?status=eq.active"
        "&order=claimed_at.desc"
        "&select=session_id,tool,repo,task_name,account,claimed_at,heartbeat_at,files_touched"
    ).format(base=_SUPABASE_URL)

    req = urllib.request.Request(url, headers={
        "apikey": _SUPABASE_KEY,
        "Authorization": "Bearer " + _SUPABASE_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = _json.loads(resp.read())
        _peer_cache = (now, rows)
        return rows
    except Exception:
        # On error, return stale cache if available, else empty
        if _peer_cache is not None:
            return _peer_cache[1]
        return []


def _get_conversation_title(pid):
    # type: (str) -> Optional[str]
    """Get the conversation title (first user message) for a session PID.

    Warp shows the conversation title in its window title bar, not the directive.
    We find it by looking up the session transcript via the session index.
    """
    ccid = f"cc-{pid}"
    # Find the most recent index entry for this ccid (index is dict: session_id → entry)
    idx = _load_index()
    session_id = None
    project_dir = None
    best_mtime = 0.0
    for sid, entry in idx.items():
        if entry.get("ccid") == ccid:
            mtime = entry.get("file_mtime", 0)
            if mtime > best_mtime:
                best_mtime = mtime
                session_id = sid
                project_dir = entry.get("project_dir")

    if not session_id or not project_dir:
        # Fallback: scan recent transcript files for this PID
        # Active sessions may not be indexed yet
        project_dirs = _project_dirs()
        candidates = []  # type: list
        for pd in project_dirs:
            try:
                for f in Path(pd).glob("*.jsonl"):
                    candidates.append((f.stat().st_mtime, f))
            except Exception:
                continue
        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, fpath in candidates[:10]:  # check 10 most recent
            try:
                with open(fpath) as fh:
                    first_line = fh.readline()
                    meta = json.loads(first_line)
                    file_sid = meta.get("sessionId", "")
                    if not file_sid:
                        continue
                    # Check if this session's ccid matches by looking at index
                    # or just try to extract the title and check later
                    title = _extract_first_user_message(fpath)
                    if title:
                        # Verify this file belongs to our PID by checking the index
                        entry = idx.get(file_sid)
                        if entry and entry.get("ccid") == ccid:
                            return title
            except Exception:
                continue
        # Last resort: check the very recent files without ccid verification
        for _, fpath in candidates[:5]:
            try:
                with open(fpath) as fh:
                    first_line = fh.readline()
                    meta = json.loads(first_line)
                    file_sid = meta.get("sessionId", "")
                    if file_sid and file_sid not in idx:
                        # Unindexed file — might be our active session
                        title = _extract_first_user_message(fpath)
                        if title:
                            # Can't confirm PID match, but it's a recent unindexed session
                            # Return it only if we have just one active unindexed session
                            return title
            except Exception:
                continue
        return None

    # Read the transcript and find the first user message
    transcript = Path(project_dir) / f"{session_id}.jsonl"
    if not transcript.exists():
        return None
    return _extract_first_user_message(transcript)


def _extract_first_user_message(fpath):
    # type: (Path) -> Optional[str]
    """Extract the first user message from a transcript file."""
    try:
        with open(fpath) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("type") == "user":
                        content = e.get("message", {}).get("content", "")
                        if isinstance(content, str):
                            return content.split("\n")[0].strip()[:80]
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    return block["text"].split("\n")[0].strip()[:80]
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _raise_warp_window(search_text):
    # type: (str) -> bool
    """Try to raise a Warp window whose title contains search_text (case-insensitive)."""
    escaped = search_text.replace("\\", "\\\\").replace('"', '\\"')
    # AppleScript 'contains' is case-insensitive by default
    script = (
        'tell application "System Events"\n'
        '  tell application process "stable"\n'
        '    set frontmost to true\n'
        '    set wl to every window whose name contains "' + escaped + '"\n'
        '    if (count of wl) > 0 then\n'
        '      perform action "AXRaise" of item 1 of wl\n'
        '      return "found"\n'
        '    end if\n'
        '  end tell\n'
        'end tell'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            timeout=3, capture_output=True, text=True,
        )
        return "found" in r.stdout
    except Exception:
        return False


def focus_session_terminal(pid):
    # type: (str) -> bool
    """Bring the Warp window for a claude session to the front.

    Tries multiple strategies to match the right Warp window:
    1. Conversation title (first user message) — matches Warp's tab title
    2. Directive text — fallback
    3. Generic Warp activation — last resort
    """
    # Strategy 1: match by conversation title (what Warp actually shows)
    title = _get_conversation_title(pid)
    if title and _raise_warp_window(title):
        return True

    # Strategy 2: match by directive
    directive = ""
    try:
        directive = Path(f"/tmp/claude-directive-{pid}").read_text().strip()
    except Exception:
        pass
    if directive and directive != "\u2014" and _raise_warp_window(directive):
        return True

    # Strategy 3: just activate Warp
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Warp" to activate'],
            timeout=3, capture_output=True,
        )
    except Exception:
        pass
    return False


def _session_last_activity(session_id):
    """Return (seconds_ago, last_tool) for a session from the ledger."""
    entries = _load_ledger(last_n=200)
    sid = f"cc-{session_id}"
    now = datetime.now(timezone.utc)
    for e in reversed(entries):
        if e.get("session") == sid and e.get("type") == "tool_use":
            try:
                ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                secs = int((now - ts).total_seconds())
                tool = e.get("tool", "?")
                if tool.startswith("mcp__claude_ai_"):
                    tool = "mcp:" + tool.replace("mcp__claude_ai_", "").replace("__", "/")
                elif tool.startswith("mcp__"):
                    tool = "mcp:" + tool[5:]
                return secs, tool
            except Exception:
                pass
    return None, None


def _detect_source(pid):
    """Detect where a Claude session was launched from via parent process."""
    try:
        r = subprocess.run(
            ['ps', '-p', pid, '-o', 'ppid='],
            capture_output=True, text=True, timeout=2,
        )
        ppid = r.stdout.strip()
        if not ppid:
            return '?'
        r2 = subprocess.run(
            ['ps', '-p', ppid, '-o', 'command='],
            capture_output=True, text=True, timeout=2,
        )
        parent_cmd = r2.stdout.strip().lower()
        if 'paperclip' in parent_cmd:
            return 'paperclip'
        if 'atlas' in parent_cmd:
            return 'atlas'
        if 'electron' in parent_cmd or 'claude desktop' in parent_cmd:
            return 'desktop'
        if 'cron' in parent_cmd or 'launchd' in parent_cmd:
            return 'scheduled'
        if any(sh in parent_cmd for sh in ('zsh', 'bash', 'fish', 'sh ')):
            return 'cli'
        return 'cli'
    except Exception:
        return '?'


# ── ledger ───────────────────────────────────────────────────────────────────

_MAX_LEDGER_CACHE = 10_000
_ledger_cache_time = 0.0
_ledger_cache = []


def _load_ledger(last_n=None):
    """Load ledger entries. Always loads all entries, caches by mtime."""
    global _ledger_cache_time, _ledger_cache
    if not LEDGER.exists():
        return []
    mtime = LEDGER.stat().st_mtime
    if mtime == _ledger_cache_time and _ledger_cache is not None:
        entries = _ledger_cache
    else:
        entries = []
        try:
            with open(LEDGER) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except Exception as e:
                            _log.debug("Malformed ledger line: %s", e)
        except Exception as e:
            _log.warning("Failed to load ledger: %s", e)
        if len(entries) > _MAX_LEDGER_CACHE:
            entries = entries[-_MAX_LEDGER_CACHE:]
        _ledger_cache = entries
        _ledger_cache_time = mtime
    if last_n is not None:
        return entries[-last_n:]
    return entries


def _interpolate_five_pct(target_ts):
    best, best_diff = None, float("inf")
    for e in _load_ledger():
        pct = e.get("five_pct")
        if pct is None:
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
            diff = abs((ts - target_ts).total_seconds())
            if diff < best_diff:
                best_diff, best = diff, pct
        except Exception:
            pass
    return best


# ── accomplishments & gravity center ────────────────────────────────────────

_HOME = str(Path.home())
_PROJECTS = str(Path.home() / "projects")

_GIT_COMMIT_RE = re.compile(r'git\s+commit\s[^|;]*?-m\s+"([^"\n$]+)"')
# Match heredoc-style commit: git commit -m "$(cat <<'EOF'\nMessage here\n..."
_GIT_COMMIT_HEREDOC_RE = re.compile(
    r"git\s+commit\s.*?-m\s+\"\$\(cat\s+<<'?EOF'?\n\s*(.+?)(?:\n|\\n)", re.DOTALL
)
_GIT_PUSH_RE = re.compile(r'git\s+push\s+\S+\s+(\S+)')
_NOISE_PATHS = {"/tmp/", ".claude/plans/", "session-index.jsonl", ".claude/directives/",
                "statusline-debug.json", "claude-directive-", "claude-token-state-"}


def _short_path(p):
    # type: (str) -> str
    """Shorten a file path for display."""
    if not p:
        return ""
    if p.startswith(_PROJECTS + "/"):
        return p[len(_PROJECTS) + 1:]
    if p.startswith(_HOME + "/"):
        return "~/" + p[len(_HOME) + 1:]
    return p


def _is_noise_path(p):
    # type: (str) -> bool
    for noise in _NOISE_PATHS:
        if noise in p:
            return True
    return False


def _classify_bash(cmd):
    # type: (str) -> Optional[str]
    """Classify a bash command as notable or None."""
    if not cmd:
        return None
    cl = cmd.lower().strip()
    if cl.startswith("echo ") or cl.startswith("cat /tmp/") or cl.startswith("ls "):
        return None
    for kw in ("deploy", "vercel ", "railway ", "npm run build", "npm run test",
               "pytest", "jest ", "npx ", "docker ", "make "):
        if kw in cl:
            return cmd.strip()[:80]
    return None


def _parse_mcp_tool(name):
    # type: (str) -> Optional[str]
    """Parse mcp tool name to 'server:action'."""
    if not name.startswith("mcp__"):
        return None
    rest = name[5:]
    if rest.startswith("claude_ai_"):
        rest = rest[10:]
    parts = rest.split("__", 1)
    if len(parts) == 2:
        return f"{parts[0]}:{parts[1]}"
    return rest


def _extract_accomplishments_from_file(f):
    # type: (Path) -> Dict[str, Any]
    """Parse a transcript file and extract accomplishments."""
    acc = {
        "files_edited": [],
        "files_created": [],
        "git_commits": [],
        "git_pushes": [],
        "skills": [],
        "mcp_ops": [],
        "bash_notable": [],
        "user_prompts": [],
        "errors": 0,
        "turn_count": 0,
    }  # type: Dict[str, Any]

    seen_files = set()  # type: set
    seen_skills = set()  # type: set
    seen_mcp = set()  # type: set
    prompt_count = 0

    try:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                t = obj.get("type", "")

                # Collect user prompts
                if t == "human":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        prompt_text = ""
                        if isinstance(content, str):
                            prompt_text = content.strip()
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    prompt_text = c.get("text", "").strip()
                                    break
                        if prompt_text and prompt_count < 5:
                            # Skip system reminders
                            if not prompt_text.startswith("<system-reminder>"):
                                acc["user_prompts"].append(prompt_text[:80])
                                prompt_count += 1

                elif t == "assistant":
                    acc["turn_count"] += 1
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue

                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue

                        name = block.get("name", "")
                        inp = block.get("input", {})
                        if not isinstance(inp, dict):
                            continue

                        # File edits
                        if name == "Edit":
                            fp = inp.get("file_path", "")
                            if fp and not _is_noise_path(fp) and fp not in seen_files:
                                seen_files.add(fp)
                                acc["files_edited"].append(_short_path(fp))

                        # File creates
                        elif name == "Write":
                            fp = inp.get("file_path", "")
                            if fp and not _is_noise_path(fp) and fp not in seen_files:
                                seen_files.add(fp)
                                acc["files_created"].append(_short_path(fp))

                        # Bash commands
                        elif name == "Bash":
                            cmd = inp.get("command", "")
                            # Git commits
                            m = _GIT_COMMIT_RE.search(cmd)
                            if not m:
                                m = _GIT_COMMIT_HEREDOC_RE.search(cmd)
                            if m:
                                msg = m.group(1).strip()
                                if msg and not msg.startswith("$"):
                                    acc["git_commits"].append(msg[:80])
                            # Git pushes
                            m2 = _GIT_PUSH_RE.search(cmd)
                            if m2:
                                acc["git_pushes"].append(m2.group(1))
                            # Notable commands
                            notable = _classify_bash(cmd)
                            if notable and len(acc["bash_notable"]) < 10:
                                acc["bash_notable"].append(notable)

                        # Skills
                        elif name == "Skill":
                            skill = inp.get("skill", "")
                            if skill and skill not in seen_skills:
                                seen_skills.add(skill)
                                acc["skills"].append(skill)

                        # Agent subagents
                        elif name == "Agent":
                            desc = inp.get("description", "")
                            if desc and len(acc["bash_notable"]) < 10:
                                acc["bash_notable"].append(f"agent: {desc}")

                        # MCP operations
                        elif name.startswith("mcp__"):
                            parsed = _parse_mcp_tool(name)
                            if parsed and parsed not in seen_mcp:
                                seen_mcp.add(parsed)
                                acc["mcp_ops"].append(parsed)

                # Check for errors in tool results
                elif t == "user":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("is_error"):
                                    acc["errors"] += 1

    except Exception:
        pass

    return acc


def _extract_accomplishments(session_id):
    # type: (str) -> Dict[str, Any]
    """Extract accomplishments for a session by ID."""
    # Check cached index first
    with _index_lock:
        snapshot = _index_cache
    entry = snapshot.get(session_id, {})
    cached = entry.get("accomplishments")
    if cached:
        return cached
    # Parse from transcript
    f = _find_transcript(session_id)
    if not f:
        return {}
    return _extract_accomplishments_from_file(f)


_MERGE_COMMIT_RE = re.compile(
    r"^Merge (pull request|branch|remote-tracking branch|tag)",
    re.IGNORECASE,
)
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(feat|fix|chore|docs|style|refactor|perf|test|ci|build|revert)"
    r"(?:\([^)]+\))?!?:\s+(.+)",
    re.IGNORECASE,
)
_GENERIC_COMMIT_WORDS = frozenset({
    "wip", "fix", "update", "fixes", "updates", "misc", "cleanup",
    "clean up", "temp", "test", "testing", "checkpoint", "progress",
    "working", "save", "draft", "todo", "fixup", "squash", "merge",
    "revert", "typo", "nit",
})


def _normalize_commit(msg):
    # type: (str) -> Optional[str]
    """Return a cleaned commit message, or None if too generic to be informative."""
    if not msg:
        return None
    stripped = msg.strip()
    # Drop merge commits
    if _MERGE_COMMIT_RE.match(stripped):
        return None
    # Strip conventional commit prefix: "feat(auth): Add login" → "Add login"
    m = _CONVENTIONAL_COMMIT_RE.match(stripped)
    if m:
        body = m.group(2).strip()
        if not body:
            return None
        stripped = body
    # Drop very short
    if len(stripped) < 5:
        return None
    # Drop single/double-word generic messages
    words = stripped.lower().split()
    if words and len(words) <= 2 and words[0] in _GENERIC_COMMIT_WORDS:
        return None
    return stripped


def _gravity_center(accomplishments, fallback=""):
    # type: (Dict[str, Any], str) -> str
    """Synthesize a short label from accomplishments."""
    if not accomplishments:
        return fallback

    # 1. Git commits — use first informative commit message
    commits = accomplishments.get("git_commits", [])
    if commits:
        good = [n for n in (_normalize_commit(c) for c in commits) if n]
        if good:
            extras = len(good) - 1
            if extras > 0:
                return f"{good[0][:50]} (+{extras} more)"
            return good[0][:60]
        # All commits were generic — fall through to other signals

    # 2. Files edited — group by top-level dir
    edited = accomplishments.get("files_edited", [])
    created = accomplishments.get("files_created", [])
    all_files = edited + created
    if all_files:
        # Find most common project prefix
        dirs = []  # type: List[str]
        for fp in all_files:
            parts = fp.split("/")
            if len(parts) >= 2:
                dirs.append(parts[0])
            else:
                dirs.append(fp)
        if dirs:
            top_dir = max(set(dirs), key=dirs.count)
            n = len(all_files)
            if n == 1:
                return f"edit {all_files[0][:55]}"
            return f"edit {n} files in {top_dir}"[:60]

    # 3. Skills used
    skills = accomplishments.get("skills", [])
    if skills:
        return " + ".join(skills[:3])[:60]

    # 4. MCP operations
    mcp = accomplishments.get("mcp_ops", [])
    if mcp:
        # Group by server
        servers = defaultdict(int)
        for op in mcp:
            srv = op.split(":")[0] if ":" in op else op
            servers[srv] += 1
        parts = [f"{c} {s}" for s, c in sorted(servers.items(), key=lambda x: x[1], reverse=True)[:3]]
        return ", ".join(parts)[:60]

    # 5. Only exploration
    prompts = accomplishments.get("user_prompts", [])
    turns = accomplishments.get("turn_count", 0)
    if turns > 0:
        if prompts:
            return prompts[0][:60]
        return f"session ({turns} turns)"

    return fallback


def _derive_project(source, project_dir, accomplishments=None):
    # type: (str, str, Optional[Dict[str, Any]]) -> str
    """Derive human-readable project name."""
    # Known source → project mappings
    if source in ("atlas-be", "atlas-fe"):
        return "atlas"
    if source == "openclaw":
        return "openclaw"
    if source == "frank":
        return "frank"
    if "/" in source:
        # Paperclip agent: SAGE/DevOp → SAGE, KAA/scheduler → KAA
        return source.split("/")[0].lower()
    if source == "paperclip":
        return "paperclip"

    # For cli sessions, infer from project_dir or files touched
    dir_name = Path(project_dir).name if project_dir else ""

    # Check project_dir for known patterns
    for name in ("atlas-backend", "atlas-portal", "atlas"):
        if name in dir_name:
            return "atlas"
    for name in ("openclaw", "frank-pilot", "paperclip", "claude-watch"):
        if name in dir_name:
            return name

    # Check files in accomplishments
    if accomplishments:
        all_files = accomplishments.get("files_edited", []) + accomplishments.get("files_created", [])
        for fp in all_files:
            fp_lower = fp.lower()
            for proj in ("claude-watch", "atlas-portal", "atlas-backend", "openclaw",
                         "frank-pilot", "paperclip", "adinkra"):
                if proj in fp_lower:
                    if "atlas" in proj:
                        return "atlas"
                    return proj

    # Home directory / general CLI
    if dir_name == "-Users-a13xperi" or source == "cli":
        return "home"

    return dir_name[:12] if dir_name else "?"


def _resolve_ccid_for_session(session_id, first_ts, last_ts):
    # type: (str, datetime, datetime) -> Optional[str]
    """Resolve CCID (cc-PID) for a session via timestamp overlap with ledger entries."""
    entries = _load_ledger(last_n=10000)

    # Build per-PID time ranges
    pid_ranges = {}  # type: Dict[str, Tuple[datetime, datetime]]
    for e in entries:
        sid = e.get("session", "")
        if not sid.startswith("cc-"):
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if sid not in pid_ranges:
            pid_ranges[sid] = (ts, ts)
        else:
            f, l = pid_ranges[sid]
            if ts < f:
                f = ts
            if ts > l:
                l = ts
            pid_ranges[sid] = (f, l)

    # Find best overlap
    if first_ts.tzinfo is None:
        first_ts = first_ts.replace(tzinfo=timezone.utc)
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)

    best_pid = None
    best_overlap = 0.0
    for pid_sid, (p_first, p_last) in pid_ranges.items():
        overlap_start = max(first_ts, p_first)
        overlap_end = min(last_ts, p_last)
        overlap = max(0.0, (overlap_end - overlap_start).total_seconds())
        if overlap > best_overlap:
            best_overlap = overlap
            best_pid = pid_sid
    if best_pid and best_overlap > 5:
        return best_pid
    return None


# ── CCID lookup ─────────────────────────────────────────────────────────────

_ccid_to_uuid = {}  # type: Dict[str, str]


def _rebuild_ccid_index():
    """Rebuild reverse CCID → UUID lookup from index cache."""
    global _ccid_to_uuid
    result = {}
    for uuid, entry in _index_cache.items():
        ccid = entry.get("ccid")
        if ccid:
            result[ccid] = uuid
    _ccid_to_uuid = result


def lookup_by_ccid(user_input):
    # type: (str) -> Optional[Dict]
    """Look up session by CCID number, cc-PID, or UUID prefix."""
    _ensure_index()
    with _index_lock:
        snapshot = _index_cache
        ccid_snapshot = _ccid_to_uuid
    s = user_input.strip()

    # Try as CCID number: "72887" → "cc-72887"
    if s.isdigit():
        s = f"cc-{s}"

    # Try as cc-PID
    if s.startswith("cc-"):
        uuid = ccid_snapshot.get(s)
        if uuid:
            return snapshot.get(uuid)
        # Fallback: scan index
        for uid, entry in snapshot.items():
            if entry.get("ccid") == s:
                return entry
        return None

    # Try as UUID prefix
    for uid, entry in snapshot.items():
        if uid.startswith(s):
            return entry

    return None


# ── session index ────────────────────────────────────────────────────────────

_index_cache = {}
_index_loaded = False
_index_building = False
_index_thread = None
_index_lock = threading.RLock()


def _load_index():
    global _index_cache, _index_loaded
    cache = {}
    if SESSION_INDEX.exists():
        try:
            with open(SESSION_INDEX) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        sid = obj.get("session_id")
                        if sid:
                            cache[sid] = obj
                    except Exception as e:
                        _log.debug("Malformed index line: %s", e)
        except Exception as e:
            _log.warning("Failed to load session index: %s", e)
    with _index_lock:
        _index_cache = cache
        _index_loaded = True
        _rebuild_ccid_index()
    return cache


def _parse_transcript(f):
    total_out = 0
    first_ts = last_ts = None
    slug = last_prompt = None
    model_counts = defaultdict(int)

    # Accomplishments tracking (inline with existing loop)
    acc = {
        "files_edited": [], "files_created": [], "git_commits": [],
        "git_pushes": [], "skills": [], "mcp_ops": [],
        "bash_notable": [], "user_prompts": [], "errors": 0, "turn_count": 0,
    }  # type: Dict[str, Any]
    seen_files = set()  # type: set
    seen_skills = set()  # type: set
    seen_mcp = set()  # type: set
    prompt_count = 0

    try:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception as e:
                    _log.debug("Transcript parse error: %s", e)
                    continue
                t = obj.get("type", "")
                ts_str = obj.get("timestamp", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    except Exception:
                        pass

                if t == "assistant":
                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    out = usage.get("output_tokens", 0)
                    total_out += out
                    mdl = msg.get("model", "")
                    if mdl and not mdl.startswith("<"):
                        model_counts[mdl] += out

                    # Extract tool_use blocks for accomplishments
                    acc["turn_count"] += 1
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            if not isinstance(inp, dict):
                                continue

                            if name == "Edit":
                                fp = inp.get("file_path", "")
                                if fp and not _is_noise_path(fp) and fp not in seen_files:
                                    seen_files.add(fp)
                                    acc["files_edited"].append(_short_path(fp))
                            elif name == "Write":
                                fp = inp.get("file_path", "")
                                if fp and not _is_noise_path(fp) and fp not in seen_files:
                                    seen_files.add(fp)
                                    acc["files_created"].append(_short_path(fp))
                            elif name == "Bash":
                                cmd = inp.get("command", "")
                                gc = _GIT_COMMIT_RE.search(cmd)
                                if not gc:
                                    gc = _GIT_COMMIT_HEREDOC_RE.search(cmd)
                                if gc:
                                    msg = gc.group(1).strip()
                                    if msg and not msg.startswith("$"):
                                        acc["git_commits"].append(msg[:80])
                                gp = _GIT_PUSH_RE.search(cmd)
                                if gp:
                                    acc["git_pushes"].append(gp.group(1))
                                notable = _classify_bash(cmd)
                                if notable and len(acc["bash_notable"]) < 10:
                                    acc["bash_notable"].append(notable)
                            elif name == "Skill":
                                skill = inp.get("skill", "")
                                if skill and skill not in seen_skills:
                                    seen_skills.add(skill)
                                    acc["skills"].append(skill)
                            elif name == "Agent":
                                desc = inp.get("description", "")
                                if desc and len(acc["bash_notable"]) < 10:
                                    acc["bash_notable"].append(f"agent: {desc}")
                            elif name.startswith("mcp__"):
                                parsed = _parse_mcp_tool(name)
                                if parsed and parsed not in seen_mcp:
                                    seen_mcp.add(parsed)
                                    acc["mcp_ops"].append(parsed)

                elif t == "human":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        prompt_text = ""
                        if isinstance(content, str):
                            prompt_text = content.strip()
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    prompt_text = c.get("text", "").strip()
                                    break
                        if prompt_text and prompt_count < 5 and not prompt_text.startswith("<system-reminder>"):
                            acc["user_prompts"].append(prompt_text[:80])
                            prompt_count += 1

                elif t == "user":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("is_error"):
                                    acc["errors"] += 1

                elif t == "system" and not slug:
                    s = obj.get("slug", "")
                    if s:
                        slug = s

                elif t == "last-prompt":
                    lp = obj.get("lastPrompt", "")
                    if lp:
                        last_prompt = lp

    except Exception:
        return None
    if first_ts is None:
        return None
    directive = (last_prompt[:40] if last_prompt else None) or slug or f.stem[:8]
    dominant_model = max(model_counts, key=model_counts.get) if model_counts else ""
    # Derive source from project directory name
    parent = f.parent.name
    m = _PAPERCLIP_RE.search(parent)
    mws = _PAPERCLIP_WS_RE.search(parent)
    if m:
        proj_uuid, agent_uuid = m.group(1), m.group(2)
        proj_info = _paperclip_map.get(proj_uuid, {})
        company = proj_info.get("company", proj_uuid[:6])
        agent_name = proj_info.get("agents", {}).get(agent_uuid, agent_uuid[:6])
        source = f"{company}/{agent_name}"
    elif mws:
        agent_uuid = mws.group(1)
        pair = _paperclip_agents_flat.get(agent_uuid)
        if pair:
            source = f"{pair[0]}/{pair[1]}"
        else:
            source = f"pp/{agent_uuid[:6]}"
    elif "atlas-backend" in parent:
        source = "atlas-be"
    elif "atlas-portal" in parent:
        source = "atlas-fe"
    elif "openclaw" in parent:
        source = "openclaw"
    elif "frank-pilot" in parent:
        source = "frank"
    elif parent == "-Users-a13xperi":
        source = "cli"
    else:
        source = "agent"

    gravity = _gravity_center(acc, directive)
    project = _derive_project(source, str(f.parent), acc)

    return {
        "session_id": f.stem,
        "first_ts": first_ts.isoformat(),
        "last_ts": (last_ts or first_ts).isoformat(),
        "output_tokens": total_out,
        "slug": slug or "",
        "directive": directive,
        "gravity": gravity,
        "project": project,
        "accomplishments": acc,
        "model": dominant_model,
        "source": source,
        "project_dir": str(f.parent),
        "file_mtime": f.stat().st_mtime,
    }


def _build_or_update_index():
    global _index_building, _index_cache
    with _index_lock:
        if _index_building:
            return
        _index_building = True
    try:
        with _index_lock:
            known = dict(_index_cache)
        new_entries = []
        for proj_dir in ALL_PROJECT_DIRS.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                sid = f.stem
                existing = known.get(sid)
                # Re-index if missing new fields or mtime changed
                needs_update = (
                    not existing
                    or f.stat().st_mtime > existing.get("file_mtime", 0)
                    or "gravity" not in existing
                )
                if not needs_update:
                    continue
                result = _parse_transcript(f)
                if result:
                    # Resolve CCID via timestamp overlap
                    if not result.get("ccid"):
                        try:
                            ft = datetime.fromisoformat(result["first_ts"])
                            lt = datetime.fromisoformat(result["last_ts"])
                            ccid = _resolve_ccid_for_session(sid, ft, lt)
                            if ccid:
                                result["ccid"] = ccid
                        except Exception:
                            pass
                    new_entries.append(result)
                    known[sid] = result
        SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
        if new_entries:
            # Atomic rewrite: temp file + rename to prevent corruption on crash
            fd, tmp_path = tempfile.mkstemp(
                dir=SESSION_INDEX.parent,
                prefix=".session-index-",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w") as fh:
                    for entry in known.values():
                        fh.write(json.dumps(entry) + "\n")
                os.replace(tmp_path, str(SESSION_INDEX))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            with _index_lock:
                _index_cache = dict(known)
                _rebuild_ccid_index()
    except Exception as e:
        _log.exception("Index build failed")
    finally:
        with _index_lock:
            _index_building = False


def _ensure_index():
    global _index_thread
    with _index_lock:
        if not _index_loaded:
            _load_index()
        if _index_thread is None or not _index_thread.is_alive():
            _index_thread = threading.Thread(target=_build_or_update_index, daemon=True)
            _index_thread.start()


def _get_session_history():
    _ensure_index()
    with _index_lock:
        snapshot = _index_cache
    # Don't exclude any sessions — show everything in history.
    # The current session appears in both Active Sessions and Session History.
    # This is better than sessions mysteriously disappearing.
    current_session_id = None

    today = datetime.now(timezone.utc).astimezone().date()
    sessions = []

    for sid, entry in snapshot.items():
        if sid == current_session_id:
            continue
        try:
            first_ts = datetime.fromisoformat(entry["first_ts"])
            last_ts = datetime.fromisoformat(entry["last_ts"])
        except Exception:
            continue

        session_date = last_ts.astimezone().date()
        secs = int((last_ts - first_ts).total_seconds())
        if secs > 86400:  # >24h — multi-day transcript, duration meaningless
            dur_str = "—"
        else:
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            dur_str = f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

        pct_str = "—"
        if session_date == today:
            ps = _interpolate_five_pct(first_ts)
            pe = _interpolate_five_pct(last_ts)
            if ps is not None and pe is not None:
                try:
                    d_pct = round(float(pe) - float(ps), 1)
                    if d_pct < -5:
                        pct_str = "↻win"  # 5h window reset during session
                    else:
                        pct_str = f"+{d_pct}%" if d_pct >= 0 else f"{d_pct}%"
                except Exception:
                    pass

        sessions.append({
            "session_id": sid,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "output_tokens": entry.get("output_tokens", 0),
            "pct_str": pct_str,
            "dur_str": dur_str,
            "directive": entry.get("gravity") or entry.get("directive", "—"),
            "slug": entry.get("slug", ""),
            "model": entry.get("model", ""),
            "source": entry.get("source", "?"),
            "project": entry.get("project", "—"),
            "date": session_date,
        })

    sessions.sort(key=lambda s: (s["last_ts"], s["session_id"]), reverse=True)
    return sessions


# ── session drill-down ───────────────────────────────────────────────────────

def _find_transcript(session_id):
    """Find transcript file for a session_id, checking index first then scanning."""
    with _index_lock:
        snapshot = _index_cache
    entry = snapshot.get(session_id)
    if entry and entry.get("project_dir"):
        p = Path(entry["project_dir"]) / f"{session_id}.jsonl"
        if p.exists():
            return p
    # Fallback: scan all project dirs
    for proj_dir in ALL_PROJECT_DIRS.iterdir():
        if not proj_dir.is_dir():
            continue
        p = proj_dir / f"{session_id}.jsonl"
        if p.exists():
            return p
    return None


_MODEL_OUTPUT_COST_PER_MTOK = {
    "opus": 75.0,
    "sonnet": 15.0,
    "haiku": 1.25,
}


def _estimate_cost(output_tokens, model_str):
    # type: (int, str) -> float
    """Estimate session cost in USD from output tokens and model name."""
    model_lower = (model_str or "").lower()
    cost_per_mtok = 15.0  # default to sonnet
    for key, rate in _MODEL_OUTPUT_COST_PER_MTOK.items():
        if key in model_lower:
            cost_per_mtok = rate
            break
    return output_tokens * cost_per_mtok / 1_000_000


def _format_cost(cost):
    # type: (float) -> str
    """Format cost as string: $0.12 or <$0.01."""
    if cost < 0.01:
        return "<$0.01"
    elif cost < 1.0:
        return f"${cost:.2f}"
    else:
        return f"${cost:.1f}"


def export_session_history_csv(filepath):
    # type: (str) -> int
    """Export session history to CSV file. Returns number of rows written."""
    with _index_lock:
        snapshot = _index_cache
    sessions = _get_session_history()
    count = 0
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "session_id", "ccid", "source", "company", "project",
            "model", "duration_min", "five_pct", "output_tokens", "cost_usd",
            "directive",
        ])
        for s in sessions:
            # Compute duration in minutes
            try:
                first_ts = s.get("first_ts")
                last_ts = s.get("last_ts")
                if first_ts and last_ts:
                    secs = int((last_ts - first_ts).total_seconds())
                    duration_min = round(secs / 60.0, 1)
                else:
                    duration_min = ""
            except Exception:
                duration_min = ""

            # Derive company from project
            project = s.get("project", "")
            p_lower = (project or "").lower().strip()
            if p_lower in ("atlas", "atlas-be", "atlas-fe"):
                company = "Delphi"
            elif p_lower in ("kaa",):
                company = "KAA"
            elif p_lower in ("frank",):
                company = "Frank"
            elif p_lower in ("openclaw", "paperclip", "claude-watch"):
                company = "Personal"
            else:
                company = ""

            # CCID from index
            idx_entry = snapshot.get(s["session_id"], {})
            ccid = idx_entry.get("ccid", "")

            out_tokens = s.get("output_tokens", 0)
            cost = _estimate_cost(out_tokens, s.get("model", ""))

            writer.writerow([
                s.get("date", ""),
                s["session_id"],
                ccid,
                s.get("source", ""),
                company,
                project,
                s.get("model", ""),
                duration_min,
                s.get("pct_str", ""),
                out_tokens,
                round(cost, 4),
                s.get("directive", ""),
            ])
            count += 1
    return count


# ── system notifications ────────────────────────────────────────────────────

NOTIFICATION_COOLDOWN = 300  # 5 min between repeat notifications
_last_notified = {}  # type: Dict[str, float]


def send_system_notification(title, body):
    # type: (str, str) -> None
    """Send a macOS system notification via osascript."""
    try:
        escaped_body = body.replace('"', '\\"')
        escaped_title = title.replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             'display notification "' + escaped_body + '" with title "' + escaped_title + '"'],
            timeout=3, capture_output=True,
        )
    except Exception:
        pass


def check_and_notify(five_pct, seven_pct, burn_rate=None):
    # type: (float, float, Optional[float]) -> None
    """Fire system notifications on spike conditions. Respects cooldown per type."""
    now = time.time()

    if five_pct > 80:
        key = "five_pct_high"
        if now - _last_notified.get(key, 0) >= NOTIFICATION_COOLDOWN:
            send_system_notification("claude-watch", "5h window at {:.0f}%".format(five_pct))
            _last_notified[key] = now

    if seven_pct > 90:
        key = "seven_pct_high"
        if now - _last_notified.get(key, 0) >= NOTIFICATION_COOLDOWN:
            send_system_notification("claude-watch", "7d window at {:.0f}%".format(seven_pct))
            _last_notified[key] = now

    if burn_rate is not None and burn_rate > 2.0:
        key = "burn_rate_high"
        if now - _last_notified.get(key, 0) >= NOTIFICATION_COOLDOWN:
            send_system_notification("claude-watch", "High burn rate: {:.1f}%/min".format(burn_rate))
            _last_notified[key] = now


def _get_session_turns(session_id):
    """Parse transcript into per-turn breakdown.
    Returns list of dicts: turn_num, tokens_in, tokens_out, model, tools, prompt_preview
    """
    f = _find_transcript(session_id)
    if not f:
        return []

    turns = []
    turn_num = 0
    current_tools = []
    last_prompt = ""

    try:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                t = obj.get("type", "")

                if t == "human":
                    # Start of a new turn — save previous if any
                    last_prompt = ""
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            last_prompt = content[:60]
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    last_prompt = c.get("text", "")[:60]
                                    break

                elif t == "assistant":
                    turn_num += 1
                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    tokens_in = usage.get("input_tokens", 0)
                    tokens_out = usage.get("output_tokens", 0)
                    model = _abbrev_model(msg.get("model", ""))

                    # Extract tool names from content blocks
                    content = msg.get("content", [])
                    tools = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tools.append(_shorten_tool(block.get("name", "?")))

                    # Estimate 5h% contribution (~5500 output tokens = 1%)
                    pct_est = tokens_out / 5500.0 if tokens_out else 0

                    turns.append({
                        "turn": turn_num,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "pct_est": round(pct_est, 2),
                        "model": model,
                        "tools": ", ".join(tools) if tools else "—",
                        "prompt": last_prompt or "—",
                    })

    except Exception:
        pass

    return turns


# ── usage metrics ────────────────────────────────────────────────────────────

def _get_usage_metrics(days=7):
    """Aggregate output tokens by source over the last N days.
    Returns (metrics_list, total_output_tokens).
    Each metric: source, output_tokens, sessions, avg_tokens, pct_of_total.
    """
    _ensure_index()
    with _index_lock:
        snapshot = _index_cache
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    by_source = defaultdict(lambda: {"output_tokens": 0, "sessions": 0})
    total_output = 0

    for sid, entry in snapshot.items():
        try:
            last_ts = datetime.fromisoformat(entry["last_ts"])
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if last_ts < cutoff:
            continue

        src = entry.get("source", "?")
        tokens = entry.get("output_tokens", 0)
        by_source[src]["output_tokens"] += tokens
        by_source[src]["sessions"] += 1
        total_output += tokens

    metrics = []
    for src, data in sorted(by_source.items(), key=lambda x: x[1]["output_tokens"], reverse=True):
        pct = (data["output_tokens"] / total_output * 100) if total_output else 0
        avg = data["output_tokens"] // data["sessions"] if data["sessions"] else 0
        metrics.append({
            "source": src,
            "output_tokens": data["output_tokens"],
            "sessions": data["sessions"],
            "avg_tokens": avg,
            "pct_of_total": pct,
        })

    return metrics, total_output


def _safe_date(ts_str):
    # type: (Optional[str]) -> Optional[object]
    """Parse an ISO timestamp string to a date object, or None on failure."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date()
    except Exception:
        return None


def _get_daily_usage(days=7):
    # type: (int) -> List[Tuple[str, int]]
    """Return (day_label, total_output_tokens) for each of the last N days, oldest first.
    Labels: 'Today' for today, abbreviated weekday name otherwise.
    """
    _ensure_index()
    with _index_lock:
        snapshot = _index_cache
    today = datetime.now().astimezone().date()
    result = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        total = sum(
            e.get("output_tokens", 0)
            for e in snapshot.values()
            if _safe_date(e.get("last_ts")) == day
        )
        label = "Today" if offset == 0 else day.strftime("%a")
        result.append((label, total))
    return result


def _get_mcp_stats(days=7):
    # type: (int) -> Dict[str, Any]
    """Aggregate MCP tool calls from ledger for the last N days.
    Returns dict with by_server, top_actions, total_calls, sessions_with_mcp.
    """
    _ensure_index()
    with _index_lock:
        snapshot = _index_cache
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    by_server = defaultdict(lambda: {"calls": 0, "actions": defaultdict(int)})  # type: Dict
    total_calls = 0

    for e in _load_ledger():
        if e.get("type") != "tool_use":
            continue
        tool = e.get("tool", "")
        if not tool.startswith("mcp__"):
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
            if ts < cutoff:
                continue
        except Exception:
            continue
        parsed = _parse_mcp_tool(tool)
        if not parsed:
            continue
        server, _, action = parsed.partition(":")
        by_server[server]["calls"] += 1
        by_server[server]["actions"][action] += 1
        total_calls += 1

    # Count sessions with any MCP usage from index
    sessions_with_mcp = 0
    for entry in snapshot.values():
        try:
            last_ts = datetime.fromisoformat(entry.get("last_ts", ""))
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if last_ts < cutoff:
                continue
        except Exception:
            continue
        if entry.get("accomplishments", {}).get("mcp_ops"):
            sessions_with_mcp += 1

    sorted_servers = []
    for server, data in sorted(by_server.items(), key=lambda x: x[1]["calls"], reverse=True):
        sorted_servers.append({
            "server": server,
            "calls": data["calls"],
            "actions": sorted(data["actions"].items(), key=lambda x: x[1], reverse=True),
        })

    all_actions = []  # type: List[Tuple[str, int]]
    for server, data in by_server.items():
        for action, count in data["actions"].items():
            all_actions.append((f"{server}:{action}", count))
    top_actions = sorted(all_actions, key=lambda x: x[1], reverse=True)[:20]

    return {
        "by_server": sorted_servers,
        "top_actions": top_actions,
        "total_calls": total_calls,
        "sessions_with_mcp": sessions_with_mcp,
    }


# ── skill stats ──────────────────────────────────────────────────────────────

def _get_skill_stats():
    """Return list of (skill_name, count, last_used_str) from ledger."""
    entries = _load_ledger(last_n=2000)
    skill_counts = defaultdict(int)
    skill_last = {}  # type: Dict[str, str]
    for e in entries:
        if e.get("type") != "tool_use":
            continue
        tool = e.get("tool", "")
        snippet = e.get("tool_snippet", "")
        if tool == "Skill":
            # snippet is the skill name (e.g. "claim-task", "paperclip")
            name = "/" + snippet.strip().split()[0].lstrip("/") if snippet.strip() else "/unknown"
            skill_counts[name] += 1
            ts = e.get("ts", "")
            try:
                skill_last[name] = datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).astimezone().strftime("%H:%M:%S")
            except Exception:
                skill_last[name] = "?"
    result = []
    for name, count in sorted(skill_counts.items(), key=lambda x: x[1], reverse=True):
        result.append((name, count, skill_last.get(name, "?")))
    return result


def _get_agent_stats(days=7):
    # type: (int) -> List[Tuple[str, int, str]]
    """Return (description_prefix, spawn_count, last_seen_str) from session index,
    aggregated over last N days, sorted by count descending.
    """
    _ensure_index()
    with _index_lock:
        snapshot = _index_cache
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    counts = defaultdict(int)  # type: Dict[str, int]
    last_seen = {}  # type: Dict[str, str]

    for sid, entry in snapshot.items():
        try:
            last_ts_str = entry.get("last_ts", "")
            last_ts = datetime.fromisoformat(last_ts_str)
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if last_ts < cutoff:
                continue
            time_str = last_ts.astimezone().strftime("%m/%d")
        except Exception:
            continue
        acc = entry.get("accomplishments", {})
        for item in acc.get("bash_notable", []):
            if not item.startswith("agent: "):
                continue
            desc = item[7:].strip()
            key = desc[:40]
            counts[key] += 1
            last_seen[key] = time_str

    result = []
    for key, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        result.append((key, count, last_seen.get(key, "?")))
    return result


def make_skills_panel():
    stats = _get_skill_stats()
    t = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 1), expand=True)
    t.add_column("Skill", overflow="ellipsis", no_wrap=True, ratio=3)
    t.add_column("Calls", min_width=5, justify="right", no_wrap=True)
    t.add_column("Last", min_width=6, no_wrap=True)
    if not stats:
        t.add_row("[dim]no skill calls yet[/dim]", "", "")
    else:
        for name, count, last in stats[:10]:
            t.add_row(name, str(count), f"[dim]{last}[/dim]")
    return Panel(t, title="[bold]Skills[/bold]  [dim](from ledger)[/dim]", border_style="magenta")


# ── PID mapping (transcript UUID → cc-PID) ───────────────────────────────────

_pid_map_cache = {}   # type: Dict[str, str]  # transcript UUID → cc-PID
_pid_map_time = 0.0


def _build_pid_map():
    """Build mapping from transcript session UUIDs to cc-PIDs using ledger timestamps."""
    global _pid_map_cache, _pid_map_time
    # Only rebuild every 10s
    now = time.time()
    if now - _pid_map_time < 10 and _pid_map_cache:
        return _pid_map_cache
    _pid_map_time = now
    with _index_lock:
        snapshot = _index_cache

    entries = _load_ledger(last_n=5000)
    # Build per-PID time ranges from ledger
    pid_ranges = {}  # type: Dict[str, Tuple[datetime, datetime]]
    for e in entries:
        sid = e.get("session", "")
        if not sid.startswith("cc-"):
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if sid not in pid_ranges:
            pid_ranges[sid] = (ts, ts)
        else:
            first, last = pid_ranges[sid]
            if ts < first:
                first = ts
            if ts > last:
                last = ts
            pid_ranges[sid] = (first, last)

    # Match transcript sessions to PIDs by overlapping time ranges
    result = {}
    for uuid, entry in snapshot.items():
        try:
            t_first = datetime.fromisoformat(entry["first_ts"])
            t_last = datetime.fromisoformat(entry["last_ts"])
            if t_first.tzinfo is None:
                t_first = t_first.replace(tzinfo=timezone.utc)
            if t_last.tzinfo is None:
                t_last = t_last.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        best_pid = None
        best_overlap = 0
        for pid_sid, (p_first, p_last) in pid_ranges.items():
            overlap_start = max(t_first, p_first)
            overlap_end = min(t_last, p_last)
            overlap = max(0, (overlap_end - overlap_start).total_seconds())
            if overlap > best_overlap:
                best_overlap = overlap
                best_pid = pid_sid
        if best_pid and best_overlap > 5:
            result[uuid] = best_pid

    _pid_map_cache = result
    return result


# ── call history (aggregated per session from ledger) ────────────────────────

def _get_call_history():
    """Aggregate tool calls per session from ledger. Returns list of dicts sorted by last activity.
    Includes recent tool details (merged from former Last Tool Activity panel).
    """
    with _index_lock:
        snapshot = _index_cache
    entries = _load_ledger(last_n=5000)
    tool_events = [e for e in entries if e.get("type") == "tool_use"]
    if not tool_events:
        return []

    sessions = {}  # type: Dict[str, Dict]
    for e in tool_events:
        sid = e.get("session", "?")
        if sid not in sessions:
            sessions[sid] = {
                "session": sid,
                "calls": 0,
                "tools": defaultdict(int),
                "first_ts": e.get("ts", ""),
                "last_ts": e.get("ts", ""),
                "directive": e.get("directive", "—"),
                "five_pct_start": e.get("five_pct"),
                "five_pct_end": e.get("five_pct"),
                "recent_tools": [],
                "model": "?",
            }
        s = sessions[sid]
        s["calls"] += 1
        tool = _shorten_tool(e.get("tool", "?"))
        s["tools"][tool] += 1
        s["last_ts"] = e.get("ts", s["last_ts"])
        pct = e.get("five_pct")
        if pct is not None:
            s["five_pct_end"] = pct
        mdl = e.get("model", "")
        if mdl and mdl != "?":
            s["model"] = mdl
        # Keep last 3 tool calls with snippets
        snippet = e.get("tool_snippet", "")
        s["recent_tools"].append(f"{tool}: {snippet[:20]}" if snippet else tool)
        if len(s["recent_tools"]) > 3:
            s["recent_tools"] = s["recent_tools"][-3:]

    result = []
    for sid, s in sessions.items():
        # Top 3 tools by count
        top_tools = sorted(s["tools"].items(), key=lambda x: x[1], reverse=True)[:3]
        tools_str = ", ".join(f"{t}({c})" for t, c in top_tools)

        # 5h% used
        try:
            delta = float(s["five_pct_end"]) - float(s["five_pct_start"])
            if delta < -5:
                pct_str = "↻win"
            else:
                pct_str = f"+{delta:.1f}%" if delta >= 0 else f"{delta:.1f}%"
        except Exception:
            pct_str = "?"

        # When (last activity)
        try:
            last = datetime.fromisoformat(s["last_ts"].replace("Z", "+00:00"))
            when_str = last.astimezone().strftime("%H:%M:%S")
            when_date = last.astimezone().date()
        except Exception:
            when_str = "?"
            when_date = None

        # Source and project from index cache
        idx_entry = snapshot.get(sid, {})
        source = idx_entry.get("source", "cli")
        project = idx_entry.get("project", "—")

        # Recent tool detail (last tool with snippet)
        recent_str = s["recent_tools"][-1] if s["recent_tools"] else "—"

        # Use gravity center for directive when available
        directive = idx_entry.get("gravity") or s["directive"] or "—"

        result.append({
            "session": sid,
            "source": source,
            "project": project,
            "model": _abbrev_model(s.get("model", "?")),
            "when": when_str,
            "when_date": when_date,
            "calls": s["calls"],
            "tools_str": tools_str,
            "recent_str": recent_str,
            "pct_str": pct_str,
            "directive": directive,
            "last_ts_raw": s["last_ts"],
        })

    result.sort(key=lambda x: x["last_ts_raw"], reverse=True)
    return result


def _get_call_data_map():
    """Return {cc_pid: {calls, tools_str, recent_str}} for merging into session history sub-rows."""
    entries = _load_ledger(last_n=5000)
    tool_events = [e for e in entries if e.get("type") == "tool_use"]
    if not tool_events:
        return {}

    sessions = {}  # type: Dict[str, Dict]
    for e in tool_events:
        sid = e.get("session", "?")
        if sid not in sessions:
            sessions[sid] = {
                "calls": 0,
                "tools": defaultdict(int),
                "recent_tools": [],
            }
        s = sessions[sid]
        s["calls"] += 1
        tool = _shorten_tool(e.get("tool", "?"))
        s["tools"][tool] += 1
        snippet = e.get("tool_snippet", "")
        s["recent_tools"].append(f"{tool}: {snippet[:20]}" if snippet else tool)
        if len(s["recent_tools"]) > 3:
            s["recent_tools"] = s["recent_tools"][-3:]

    result = {}
    for sid, s in sessions.items():
        top_tools = sorted(s["tools"].items(), key=lambda x: x[1], reverse=True)[:3]
        tools_str = ", ".join(f"{t}({c})" for t, c in top_tools)
        recent_str = s["recent_tools"][-1] if s["recent_tools"] else ""
        result[sid] = {
            "calls": s["calls"],
            "tools_str": tools_str,
            "recent_str": recent_str,
        }
    return result


# ── tool feed rows ───────────────────────────────────────────────────────────

def _shorten_tool(tool):
    if tool.startswith("mcp__claude_ai_"):
        return "mcp:" + tool.replace("mcp__claude_ai_", "").replace("__", "/")
    if tool.startswith("mcp__"):
        return "mcp:" + tool[5:]
    return tool


def _compute_tool_feed_rows(last_n=200):
    """Return list of dicts with display-ready fields for tool feed.
    Each dict: ts_str, session, tool, directive, delta_str, delta_style
    """
    with _index_lock:
        snapshot = _index_cache
    entries = _load_ledger(last_n=500)
    tool_events = [e for e in entries if e.get("type") == "tool_use"][-last_n:]
    if not tool_events:
        return []

    # Build prev_pct map (first seen pct per session)
    prev_pct = {}
    for e in tool_events:
        sess = e.get("session", "?")
        pct = e.get("five_pct")
        if pct is not None and sess not in prev_pct:
            prev_pct[sess] = pct

    rows = []
    for e in reversed(tool_events):
        ts = e.get("ts", "")
        try:
            ts_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
        except Exception:
            ts_str = ts[-8:] if ts else "?"

        session = e.get("session", "?")
        tool = _shorten_tool(e.get("tool", "?"))
        directive = e.get("directive", "—") or "—"
        if directive == "unknown":
            directive = "—"

        cur_pct = e.get("five_pct")
        cumulative = e.get("delta_from_start", 0)

        prev = prev_pct.get(session)
        tick = None
        if cur_pct is not None and prev is not None:
            try:
                diff = float(cur_pct) - float(prev)
                if diff > 0:
                    tick = diff
            except Exception:
                pass
        if cur_pct is not None:
            prev_pct[session] = cur_pct

        if tick:
            delta_str = f"▲+{tick:.0f}%"
            delta_style = "bold red" if tick >= 2 else "bold yellow"
        elif cumulative:
            try:
                c = float(cumulative)
                delta_str = f"+{c:.1f}%" if c > 0 else "—"
                delta_style = "dim"
            except Exception:
                delta_str = "—"
                delta_style = "dim"
        else:
            delta_str = "—"
            delta_style = "dim"

        snippet = e.get("tool_snippet", "")
        # Strip cc- prefix from session for index lookup
        index_sid = session[3:] if session.startswith("cc-") else session
        source = snapshot.get(index_sid, {}).get("source", "cli")
        rows.append({
            "ts_str": ts_str,
            "session": session,
            "tool": f"{tool}: {snippet[:15]}" if snippet else tool,
            "directive": directive,
            "delta_str": delta_str,
            "delta_style": delta_style,
            "source": source,
        })

    return rows


# ── drain ────────────────────────────────────────────────────────────────────

def _drain_status(drain_events):
    if not drain_events:
        return "dim", "● No drain data yet"
    last = drain_events[-1]
    try:
        delta = float(last.get("delta_5h", 0))
        burn = float(last.get("burn_rate_per_min", 0))
        sessions = int(last.get("cli_sessions", 0))
    except Exception:
        return "dim", "● Status unknown"

    if delta > 3:
        return "red", f"✖  Spike — +{delta:.0f}% in one interval. Check for runaway."
    if burn > 6:
        return "red", f"✖  Runaway — {burn:.1f}%/min burn rate detected"
    if sessions > 2:
        per = burn / sessions if sessions else burn
        return "yellow", f"▲  {sessions} sessions — ~{per:.1f}%/min each"
    return "green", f"●  Normal — {sessions} session{'s' if sessions != 1 else ''}, ~{burn:.0f}%/min"


# ── burndown chart data ─────────────────────────────────────────────────────

_burndown_cache = None  # type: Optional[Dict]
_burndown_cache_time = 0.0


def _get_burndown_data():
    # type: () -> Dict[str, Any]
    """Compute burndown chart data for current 5h window."""
    global _burndown_cache, _burndown_cache_time
    now = time.time()
    if _burndown_cache and now - _burndown_cache_time < 30:
        return _burndown_cache

    five, _, five_reset_ts, _ = _current_pct()
    if five == "?" or not five_reset_ts:
        return {}

    try:
        reset = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        window_start = reset - timedelta(hours=5)
        mins_total = 300.0  # 5 hours
        mins_elapsed = max(0, (now_utc - window_start).total_seconds() / 60)
        mins_to_reset = max(0, (reset - now_utc).total_seconds() / 60)
        remaining_pct = 100.0 - float(five)
    except Exception:
        return {}

    # Load ledger and bucket actual data at 2-min intervals
    entries = _load_ledger()
    raw_points = []  # type: List[Tuple[float, float]]  # (mins_elapsed, remaining_pct)
    for e in entries:
        if e.get("type") != "tool_use":
            continue
        pct = e.get("five_pct")
        if pct is None:
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
            if ts < window_start:
                continue
            elapsed = (ts - window_start).total_seconds() / 60
            raw_points.append((elapsed, 100.0 - float(pct)))
        except Exception:
            continue

    # Bucket into 2-minute intervals
    bucket_size = 2.0
    num_buckets = int(mins_elapsed / bucket_size) + 1
    actual = []  # type: List[Tuple[float, float]]
    for i in range(num_buckets):
        bucket_min = i * bucket_size
        bucket_max = bucket_min + bucket_size
        pts = [r for m, r in raw_points if bucket_min <= m < bucket_max]
        if pts:
            actual.append((bucket_min + bucket_size / 2, pts[-1]))
        elif actual:
            actual.append((bucket_min + bucket_size / 2, actual[-1][1]))

    # If no data at all, start with 100%
    if not actual:
        actual = [(0, 100.0)]

    # Ideal pace: straight line from 100% at start to 0% at reset
    ideal = []  # type: List[Tuple[float, float]]
    for i in range(num_buckets + int(mins_to_reset / bucket_size) + 1):
        m = i * bucket_size
        ideal_remaining = max(0, 100.0 * (1.0 - m / mins_total))
        ideal.append((m, ideal_remaining))

    # Current rate: average over last 10 minutes
    current_rate = 0.0
    recent = [(m, r) for m, r in raw_points if m > mins_elapsed - 10]
    if len(recent) >= 2:
        delta_pct = recent[0][1] - recent[-1][1]  # remaining dropped by this much
        delta_mins = recent[-1][0] - recent[0][0]
        if delta_mins > 0:
            current_rate = delta_pct / delta_mins  # %/min consumed

    # Projection
    projected_wall_mins = None  # type: Optional[float]
    projected_remaining_at_reset = remaining_pct
    if current_rate > 0:
        projected_wall_mins = remaining_pct / current_rate
        projected_remaining_at_reset = max(0, remaining_pct - current_rate * mins_to_reset)

    # Status
    if projected_wall_mins is not None and projected_wall_mins < 15:
        status = "critical"
    elif projected_wall_mins is not None and projected_wall_mins < mins_to_reset:
        status = "burning_fast"
    elif mins_to_reset < 30 and remaining_pct > 30:
        status = "wasting"
    else:
        status = "on_track"

    result = {
        "actual": actual,
        "ideal": ideal,
        "projected_wall_mins": projected_wall_mins,
        "projected_remaining_at_reset": projected_remaining_at_reset,
        "current_rate": current_rate,
        "remaining_pct": remaining_pct,
        "mins_to_reset": mins_to_reset,
        "mins_elapsed": mins_elapsed,
        "mins_total": mins_total,
        "window_start": window_start,
        "window_reset": reset,
        "status": status,
    }  # type: Dict[str, Any]
    _burndown_cache = result
    _burndown_cache_time = now
    return result


# ── token attribution ──────────────────────────────────────────────────────

_attribution_cache = None  # type: Optional[Dict]
_attribution_cache_time = 0.0

_ATTR_COLORS = ["red", "dodgerblue", "green", "yellow", "magenta", "cyan", "dark_orange", "deep_pink"]


def _get_token_attribution():
    # type: () -> Dict[str, Any]
    """Compute per-session token consumption breakdown for current 5h window."""
    global _attribution_cache, _attribution_cache_time
    with _index_lock:
        snapshot = _index_cache
    now = time.time()
    if _attribution_cache and now - _attribution_cache_time < 30:
        return _attribution_cache

    five, _, five_reset_ts, _ = _current_pct()
    if five == "?" or not five_reset_ts:
        return {}

    try:
        reset = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        window_start = reset - timedelta(hours=5)
        current_five_pct = float(five)
    except Exception:
        return {}

    # Load ledger entries in window, filter to tool_use
    entries = _load_ledger()
    window_entries = []  # type: List[Dict]
    for e in entries:
        if e.get("type") != "tool_use":
            continue
        pct = e.get("five_pct")
        if pct is None:
            continue
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
            if ts < window_start:
                continue
            window_entries.append({
                "ts": ts,
                "session": e.get("session", ""),
                "directive": e.get("directive", ""),
                "five_pct": float(pct),
                "output_tokens": e.get("output_tokens", 0),
                "model": e.get("model", ""),
                "tool": e.get("tool", ""),
            })
        except Exception:
            continue

    if not window_entries:
        return {}

    # Backfill empty session IDs using snapshot
    for we in window_entries:
        if we["session"]:
            continue
        ts = we["ts"]
        directive = we["directive"]
        # Try directive + timestamp match
        matched = False
        if directive:
            for sid, entry in snapshot.items():
                try:
                    ft = datetime.fromisoformat(entry["first_ts"].replace("Z", "+00:00"))
                    lt = datetime.fromisoformat(entry["last_ts"].replace("Z", "+00:00"))
                    if ft.tzinfo is None:
                        ft = ft.replace(tzinfo=timezone.utc)
                    if lt.tzinfo is None:
                        lt = lt.replace(tzinfo=timezone.utc)
                    if ft <= ts <= lt + timedelta(minutes=5):
                        e_dir = entry.get("directive", "") or entry.get("gravity", "")
                        if e_dir and directive.lower() in e_dir.lower():
                            we["session"] = sid
                            matched = True
                            break
                except Exception:
                    continue
        # Fallback: timestamp overlap only
        if not matched:
            for sid, entry in snapshot.items():
                try:
                    ft = datetime.fromisoformat(entry["first_ts"].replace("Z", "+00:00"))
                    lt = datetime.fromisoformat(entry["last_ts"].replace("Z", "+00:00"))
                    if ft.tzinfo is None:
                        ft = ft.replace(tzinfo=timezone.utc)
                    if lt.tzinfo is None:
                        lt = lt.replace(tzinfo=timezone.utc)
                    if ft <= ts <= lt + timedelta(minutes=5):
                        we["session"] = sid
                        break
                except Exception:
                    continue

    # Group remaining unmatched by directive
    for we in window_entries:
        if not we["session"]:
            d = we["directive"] or "unknown"
            we["session"] = "unknown-" + d

    # Sort by timestamp
    window_entries.sort(key=lambda e: e["ts"])

    # Compute per-session consumption using consecutive-delta method
    session_deltas = defaultdict(float)  # type: Dict[str, float]
    session_meta = {}  # type: Dict[str, Dict]

    for we in window_entries:
        sid = we["session"]
        if sid not in session_meta:
            session_meta[sid] = {
                "directive": we["directive"],
                "first_ts": we["ts"],
                "last_ts": we["ts"],
                "output_tokens": 0,
                "model_counts": defaultdict(int),
                "tool_count": 0,
            }
        meta = session_meta[sid]
        meta["last_ts"] = we["ts"]
        meta["output_tokens"] += we.get("output_tokens", 0)
        if we["model"]:
            meta["model_counts"][we["model"]] += 1
        meta["tool_count"] += 1

    # Consecutive deltas
    for i in range(1, len(window_entries)):
        prev = window_entries[i - 1]
        curr = window_entries[i]
        delta = curr["five_pct"] - prev["five_pct"]
        if delta > 0:
            session_deltas[curr["session"]] += delta

    # Build session list
    total_attributed = sum(session_deltas.values())
    unaccounted = max(0, current_five_pct - total_attributed)

    sessions = []  # type: List[Dict]
    color_idx = 0
    for sid, meta in session_meta.items():
        pct_used = session_deltas.get(sid, 0)
        model_counts = meta["model_counts"]
        dominant_model = max(model_counts, key=model_counts.get) if model_counts else "?"
        sessions.append({
            "session_id": sid,
            "directive": meta["directive"],
            "first_ts": meta["first_ts"],
            "last_ts": meta["last_ts"],
            "pct_used": round(pct_used, 1),
            "output_tokens": meta["output_tokens"],
            "model": _abbrev_model(dominant_model),
            "tool_count": meta["tool_count"],
            "color": _ATTR_COLORS[color_idx % len(_ATTR_COLORS)],
        })
        color_idx += 1

    # Sort by pct_used descending
    sessions.sort(key=lambda s: s["pct_used"], reverse=True)
    # Re-assign colors after sort so top consumers get first colors
    for i, s in enumerate(sessions):
        s["color"] = _ATTR_COLORS[i % len(_ATTR_COLORS)]

    result = {
        "total_used_pct": round(current_five_pct, 1),
        "unaccounted_pct": round(unaccounted, 1),
        "sessions": sessions,
    }  # type: Dict[str, Any]
    _attribution_cache = result
    _attribution_cache_time = now
    return result


# ── system health ───────────────────────────────────────────────────────────

_SYSTEM_MEM_MB = 16384  # default, updated on first call
_health_cache = None  # type: Optional[Dict]
_health_cache_time = 0.0

# Process name → display label mapping
_INFRA_NAMES = {
    "Virtual Machine Service for Claude": "VM Svc Claude",
    "stable": "Warp",
    "Notion Helper (Renderer)": "Notion",
    "Notion Helper": "Notion",
    "Notion": "Notion",
    "chrome-headless-shell": "chrome-headless",
    "node": "node",
    "Claude Helper (Renderer)": "Claude Desktop",
    "Claude Helper": "Claude Desktop",
}


def _get_system_health():
    # type: () -> Dict[str, Any]
    """Return system health snapshot from ps."""
    global _health_cache, _health_cache_time, _SYSTEM_MEM_MB
    now = time.time()
    if _health_cache and now - _health_cache_time < 5:
        return _health_cache

    # Get system memory once
    try:
        r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2)
        _SYSTEM_MEM_MB = int(r.stdout.strip()) // (1024 * 1024)
    except Exception:
        pass

    # Get all processes
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,pcpu,rss,etime,comm"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return {}

    # Get active session info for cross-referencing
    active = _active_sessions()
    active_pids = {item[0]: item[2] for item in active}  # pid → directive
    active_sources = {item[0]: (item[4] if len(item) > 4 else "?") for item in active}  # pid → source

    claude_sessions = []  # type: List[Dict]
    infra_raw = defaultdict(lambda: {"cpu": 0.0, "mem_mb": 0.0, "pids": [], "count": 0})
    total_cpu = 0.0
    total_mem = 0.0

    now_dt = datetime.now()

    for line in r.stdout.splitlines()[1:]:  # skip header
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = parts[0]
            cpu = float(parts[1])
            mem_kb = int(parts[2])
            etime_str = parts[3]
            comm = parts[4].strip()
        except Exception:
            continue

        # Parse etime (formats: MM:SS, HH:MM:SS, D-HH:MM:SS)
        try:
            elapsed_secs = _etime_to_secs(etime_str)
            start_time = (now_dt - timedelta(seconds=elapsed_secs)).strftime("%H:%M:%S") if elapsed_secs else "?"
        except Exception:
            start_time = "?"

        mem_mb = mem_kb / 1024.0

        # Is this a Claude CLI session?
        comm_base = comm.rsplit("/", 1)[-1] if "/" in comm else comm
        if comm_base == "claude":
            directive = active_pids.get(pid, "—")
            is_active = pid in active_pids
            status = "active" if is_active else "exited"
            if is_active and cpu > 20:
                # Check if idle (no recent tool call)
                secs, _ = _session_last_activity(pid)
                if secs and secs > 300:
                    status = "runaway"
            source = active_sources.get(pid, "?")
            claude_sessions.append({
                "pid": pid, "cpu": cpu, "mem_mb": round(mem_mb),
                "directive": directive, "status": status,
                "start_time": start_time, "source": source,
            })
            total_cpu += cpu
            total_mem += mem_mb
            continue

        # Check against infrastructure names
        for pattern, label in _INFRA_NAMES.items():
            if pattern in comm:
                infra_raw[label]["cpu"] += cpu
                infra_raw[label]["mem_mb"] += mem_mb
                infra_raw[label]["pids"].append(pid)
                infra_raw[label]["count"] += 1
                total_cpu += cpu
                total_mem += mem_mb
                break

    # Build infrastructure list
    infrastructure = []  # type: List[Dict]
    for name, data in sorted(infra_raw.items(), key=lambda x: x[1]["mem_mb"], reverse=True):
        entry = {
            "name": name,
            "cpu": round(data["cpu"], 1),
            "mem_mb": round(data["mem_mb"]),
            "count": data["count"],
            "pid": data["pids"][0] if data["count"] == 1 else "—",
        }  # type: Dict[str, Any]
        infrastructure.append(entry)

    # Sort claude sessions by memory desc
    claude_sessions.sort(key=lambda x: x["mem_mb"], reverse=True)

    # Alerts
    alerts = []  # type: List[str]
    for s in claude_sessions:
        if s["status"] == "runaway":
            alerts.append(f"cc-{s['pid']} runaway: {s['cpu']:.0f}% CPU while idle >5m")
    for inf in infrastructure:
        if inf["mem_mb"] > 3000:
            count_str = f" across {inf['count']} processes" if inf["count"] > 1 else ""
            alerts.append(f"{inf['name']} using {inf['mem_mb']/1024:.1f}GB{count_str}")
        if inf["cpu"] > 50:
            alerts.append(f"{inf['name']} at {inf['cpu']:.0f}% CPU")

    mem_pct = (total_mem / _SYSTEM_MEM_MB * 100) if _SYSTEM_MEM_MB else 0

    result = {
        "claude_sessions": claude_sessions,
        "infrastructure": infrastructure,
        "totals": {
            "cpu": round(total_cpu, 1),
            "mem_mb": round(total_mem),
            "mem_pct": round(mem_pct, 1),
            "system_mem_mb": _SYSTEM_MEM_MB,
        },
        "alerts": alerts,
    }  # type: Dict[str, Any]
    _health_cache = result
    _health_cache_time = now
    return result


# ── Rich panel builders (used by Rich version + Textual Static widgets) ──────

def _token_pacing():
    """Predict time to 100% based on recent burn rates."""
    entries = _load_ledger(last_n=200)
    drains = [e for e in entries if e.get("type") == "tool_drain" and e.get("delta_5h", 0) > 0][-5:]
    if not drains:
        return None
    
    avg_burn = sum(d.get("burn_rate_per_min", 0) for d in drains) / len(drains)
    if avg_burn <= 0:
        return None
    
    five, _, five_reset_ts, _ = _current_pct()
    try:
        remaining = 100 - float(five)
    except Exception:
        return None
    
    if remaining <= 0:
        return {"status": "at_limit", "mins_to_reset": 0, "avg_burn": avg_burn}
    
    mins_to_100 = remaining / avg_burn
    
    try:
        reset = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
        mins_to_reset = max(0, (reset - datetime.now(timezone.utc)).total_seconds() / 60)
    except Exception:
        mins_to_reset = 0
    
    return {
        "status": "pacing",
        "mins_to_100": mins_to_100,
        "mins_to_reset": mins_to_reset,
        "avg_burn": avg_burn,
        "remaining_pct": remaining,
    }


def _get_active_account():
    """Return (label, name, lane) for active account."""
    try:
        d = json.loads((Path.home() / ".claude/accounts.json").read_text())
        active = d.get("active", "?")
        for acct in d.get("accounts", []):
            if acct.get("label") == active:
                return active, acct.get("name", "?"), acct.get("lane", "?")
        return active, "?", "?"
    except Exception:
        return "?", "?", "?"


def _get_all_account_capacities():
    # type: () -> list
    """Return capacity info for all accounts. Live data only for active account."""
    five, seven, five_reset_ts, seven_reset_ts = _current_pct()
    try:
        d = json.loads((Path.home() / ".claude/accounts.json").read_text())
        active_label = d.get("active", "?")
        accounts = d.get("accounts", [])
    except Exception:
        return []

    result = []
    for acct in accounts:
        label = acct.get("label", "?")
        is_active = label == active_label
        result.append({
            "label": label,
            "name": acct.get("name", "?"),
            "lane": acct.get("lane", "?"),
            "active": is_active,
            "five_pct": five if is_active else "—",
            "seven_pct": seven if is_active else "—",
            "five_reset": five_reset_ts if is_active else "",
            "seven_reset": seven_reset_ts if is_active else "",
        })
    return result


def _get_supabase_account_capacity():
    # type: () -> List[Dict[str, Any]]
    """Fetch account capacity snapshots from Supabase.

    Returns list of dicts with columns: account, account_name,
    five_hour_used_pct, five_hour_resets_at, seven_day_used_pct,
    seven_day_resets_at, snapshot_at, is_active.
    """
    import urllib.request
    import json as _json

    url = (
        "https://zoirudjyqfqvpxsrxepr.supabase.co/rest/v1/account_capacity"
        "?order=account.asc"
    )
    key = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvaXJ1ZGp5cWZxdnB4c3J4ZXByIiwi"
        "cm9sZSI6ImFub24iLCJpYXQiOjE3NjgwMzE4MjgsImV4cCI6MjA4MzYwNzgyOH0."
        "6W6OzRfJ-nmKN_23z1OBCS4Cr-ODRq9DJmF_yMwOCfo"
    )

    req = urllib.request.Request(url, headers={
        "apikey": key,
        "Authorization": "Bearer " + key,
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception:
        return []


def get_account_capacity_display():
    # type: () -> List[Dict[str, Any]]
    """Combine Supabase capacity data, live data for active account, and
    accounts.json metadata.  Returns list of dicts ready for display:

        label, name, lane, repos, is_active, five_pct, seven_pct,
        five_reset, seven_reset, snapshot_age_min
    """
    # 1. Live data for active account
    five_live, seven_live, five_reset_live, seven_reset_live = _current_pct()

    # 2. accounts.json metadata
    try:
        accts_json = json.loads(
            (Path.home() / ".claude/accounts.json").read_text()
        )
        active_label = accts_json.get("active", "?")
        accounts_meta = {
            a.get("label", "?"): a for a in accts_json.get("accounts", [])
        }
    except Exception:
        active_label = "?"
        accounts_meta = {}

    # 3. Supabase snapshots
    sb_rows = _get_supabase_account_capacity()
    sb_map = {}  # type: Dict[str, Dict[str, Any]]
    for row in sb_rows:
        sb_map[row.get("account", "?")] = row

    # Build result for A, B, C
    result = []  # type: List[Dict[str, Any]]
    for label in ("A", "B", "C"):
        meta = accounts_meta.get(label, {})
        sb = sb_map.get(label, {})
        is_active = label == active_label

        # Compute snapshot age in minutes
        snap_age = None  # type: Optional[float]
        snap_at = sb.get("snapshot_at")
        if snap_at:
            try:
                snap_dt = datetime.fromisoformat(
                    snap_at.replace("Z", "+00:00")
                )
                snap_age = (
                    datetime.now(timezone.utc) - snap_dt
                ).total_seconds() / 60.0
            except Exception:
                snap_age = None

        if is_active:
            # Use live data — it is fresher
            five_pct = five_live
            seven_pct = seven_live
            five_reset = five_reset_live
            seven_reset = seven_reset_live
            age_min = 0.0
        else:
            five_pct = sb.get("five_hour_used_pct", "—")
            seven_pct = sb.get("seven_day_used_pct", "—")
            five_reset = sb.get("five_hour_resets_at", "")
            seven_reset = sb.get("seven_day_resets_at", "")
            age_min = snap_age if snap_age is not None else -1.0

        result.append({
            "label": label,
            "name": meta.get("name", sb.get("account_name", "?")),
            "lane": meta.get("lane", "?"),
            "repos": meta.get("repos", []),
            "is_active": is_active,
            "five_pct": five_pct,
            "seven_pct": seven_pct,
            "five_reset": five_reset,
            "seven_reset": seven_reset,
            "snapshot_age_min": age_min,
        })

    return result


def _burn_mode():
    """Return burn mode state: (active, remaining_secs) or (False, 0)."""
    burn_file = Path("~/.claude/burn-mode.json").expanduser()
    try:
        with open(burn_file) as f:
            data = json.load(f)
        now = time.time()
        if data.get("active") and data.get("expires", 0) > now:
            return True, int(data["expires"] - now)
    except Exception:
        pass
    return False, 0


def make_header(five, seven, five_reset_ts, seven_reset_ts):
    budget = _budget()
    def bar(pct, width=20):
        try:
            pct_f = float(pct)
            filled = int(pct_f * width / 100)
            color = "green" if pct_f < 50 else ("yellow" if pct_f < 75 else "red")
            pct_display = f"{pct_f:.1f}" if pct_f != int(pct_f) else str(int(pct_f))
        except Exception:
            filled, color, pct_display = 0, "dim", "?"
        return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}] {pct_display}%"

    t = Table.grid(padding=(0, 2))
    t.add_column(justify="left")
    t.add_column(justify="left")
    t.add_row(
        f"[bold]5h window[/bold]   {bar(five)}",
        f"[bold]7d window[/bold]   {bar(seven)}",
    )
    t.add_row(
        f"resets in [cyan]{_countdown(five_reset_ts)}[/cyan]",
        f"resets [cyan]{_reset_day(seven_reset_ts)}[/cyan]",
    )
    label, name, lane = _get_active_account()
    acct_color = "cyan" if label == "A" else ("magenta" if label == "B" else "yellow")
    t.add_row(
        f"[cyan]{datetime.now().strftime('%H:%M:%S')}[/cyan]  [dim]Last updated[/dim]",
        f"[{acct_color}]Account {label}[/{acct_color}]: {name} [dim]({lane})[/dim]",
    )
    # Token pacing prediction
    pacing = _token_pacing()
    if pacing:
        if pacing["status"] == "at_limit":
            pace_str = f"[red]AT LIMIT[/red] — reset in {_countdown(five_reset_ts)}"
        else:
            m100 = pacing["mins_to_100"]
            mr = pacing["mins_to_reset"]
            burn = pacing["avg_burn"]
            if m100 < mr:
                pace_str = f"[yellow]100% in ~{m100:.0f}m[/yellow] at {burn:.1f}%/min"
            else:
                headroom = mr - m100
                pace_str = f"[green]~{pacing['remaining_pct']:.0f}% left[/green] at {burn:.1f}%/min — resets first"
        t.add_row(pace_str, "")

    burn_active, burn_secs = _burn_mode()
    title = "[bold white]Token Monitor[/bold white]"
    if burn_active:
        burn_min = burn_secs // 60
        burn_sec = burn_secs % 60
        title += f"  [bold magenta]BURN MODE {burn_min}m {burn_sec:02d}s[/bold magenta]"
    return Panel(t, title=title, border_style="bright_blue")


def make_urgent_panel():
    """Return urgent alerts panel, or None if nothing urgent."""
    five, seven, five_reset_ts, seven_reset_ts = _current_pct()
    
    alerts = []
    
    # Check 5h window — unallocated tokens expiring soon
    try:
        reset = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
        mins_left = int((reset - datetime.now(timezone.utc)).total_seconds() / 60)
        pct_used = float(five)
        pct_remaining = 100 - pct_used
        
        if mins_left <= 30 and pct_remaining >= 1:
            if mins_left <= 5:
                urgency = "[bold red blink]CRITICAL[/bold red blink]"
                color = "red"
            elif mins_left <= 10:
                urgency = "[bold red]URGENT[/bold red]"
                color = "red"
            elif mins_left <= 15:
                urgency = "[bold yellow]WARNING[/bold yellow]"
                color = "yellow"
            else:
                urgency = "[yellow]HEADS UP[/yellow]"
                color = "yellow"
            
            alerts.append(
                f"  {urgency} — [bold]{pct_remaining:.0f}% tokens unused[/bold], "
                f"resets in [{color}]{mins_left}m[/{color}]. Use them or lose them."
            )
    except Exception:
        pass

    # Check for runaway burn rate from drain events — with actionable detail
    try:
        entries = _load_ledger(last_n=200)
        drain_events = [e for e in entries if e.get("type") == "tool_drain" and e.get("delta_5h", 0) > 0][-5:]
        if drain_events:
            last = drain_events[-1]
            burn = float(last.get("burn_rate_per_min", 0))
            num_sessions = int(last.get("cli_sessions", 0))
            delta = float(last.get("delta_5h", 0))

            if burn > 6 or (burn > 3 and num_sessions >= 2):
                # Identify the top burner from active sessions
                active = _active_sessions()
                top_pid = None
                top_delta = 0
                top_directive = "—"
                top_idle_secs = 0
                for item in active:
                    pid, _, directive, delta_str = item[0], item[1], item[2], item[3]
                    try:
                        d = float(delta_str.strip("+%"))
                    except Exception:
                        d = 0
                    if d > top_delta:
                        top_delta = d
                        top_pid = pid
                        top_directive = directive
                        secs, _ = _session_last_activity(pid)
                        top_idle_secs = secs or 0

                severity = "[bold red]RUNAWAY[/bold red]" if burn > 6 else "[yellow]HIGH BURN[/yellow]"
                line1 = (
                    f"  {severity} — {burn:.1f}%/min across "
                    f"{num_sessions} session{'s' if num_sessions != 1 else ''}."
                )
                alerts.append(line1)

                if top_pid:
                    idle_m = top_idle_secs // 60
                    line2 = (
                        f"  Top burner: [bold cyan]cc-{top_pid}[/bold cyan] "
                        f"at [bold]+{top_delta:.0f}%[/bold] "
                        f"({top_directive[:25]})"
                    )
                    if top_idle_secs > 300:
                        line2 += (
                            f" — [bold red]idle {idle_m}m[/bold red]. "
                            f"Likely stuck. Run: [bold]kill {top_pid}[/bold]"
                        )
                    elif top_idle_secs > 60:
                        line2 += f" — idle {idle_m}m. Monitor or close if unneeded."
                    else:
                        line2 += " — actively working."
                    alerts.append(line2)
    except Exception:
        pass

    if not alerts:
        return None

    from rich.text import Text
    t = Table(box=None, padding=(0, 0), expand=True, show_header=False)
    t.add_column(ratio=1)
    for alert in alerts:
        t.add_row(alert)
    
    return Panel(t, title="[bold red]⚠ URGENT[/bold red]", border_style="red")


def _etime_to_secs(etime):
    """Parse ps etime string ([[DD-]HH:]MM:SS) to total seconds."""
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = etime.split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        else:
            return None
        return days * 86400 + h * 3600 + m * 60 + s
    except Exception:
        return None


def make_sessions_panel():
    """Active Sessions with inline call detail sub-rows."""
    sessions = _active_sessions()
    entries = _load_ledger(last_n=500)
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()

    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1), expand=True)
    t.add_column("When", width=9, no_wrap=True)
    t.add_column("Session", width=10, no_wrap=True)
    t.add_column("Src", width=10, no_wrap=True)
    t.add_column("Project", width=12, no_wrap=True)
    t.add_column("Mdl", width=10, no_wrap=True)
    t.add_column("Dur", width=12, no_wrap=True)
    t.add_column("Used", width=11, no_wrap=True)
    t.add_column("Directive", overflow="ellipsis", no_wrap=True)

    n = len(sessions)
    title = f"[bold]Active Sessions[/bold]  [dim](live)[/dim] — {n}" if n else "[bold]Active Sessions[/bold]  [dim](live)[/dim]"

    if not sessions:
        t.add_row("", "[dim]—[/dim]", "", "", "", "", "", "[dim]no active sessions[/dim]")
        return Panel(t, title=title, border_style="cyan")

    # Single-pass ledger scan: build model, last call, first output per session
    model_map = {}    # type: Dict[str, str]
    last_call = {}    # type: Dict[str, Tuple[datetime, str, int]]
    first_out = {}    # type: Dict[str, int]
    for e in entries:
        sid = e.get("session", "")
        if not sid:
            continue
        mdl = e.get("model")
        if mdl and mdl != "?":
            model_map[sid] = mdl
        if e.get("type") == "tool_use":
            if sid not in first_out:
                first_out[sid] = e.get("output_tokens", 0)
            try:
                ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                tool = _shorten_tool(e.get("tool", "?"))
                out = e.get("output_tokens", 0)
                last_call[sid] = (ts, tool, out)
            except Exception:
                pass

    for item in sessions:
        pid, age, directive, delta = item[0], item[1], item[2], item[3]
        source = item[4] if len(item) > 4 else "?"
        sid = f"cc-{pid}"

        # ── Header row ──
        elapsed_s = _etime_to_secs(age)
        start_str = (now_local - timedelta(seconds=elapsed_s)).strftime("%H:%M:%S") if elapsed_s else "?"

        color = "green"
        if delta == "new":
            color = "dim"
        else:
            try:
                val = float(delta.strip("+%"))
                color = "red" if val > 10 else ("yellow" if val > 5 else "green")
            except Exception:
                pass

        mdl = _abbrev_model(model_map.get(sid, "?"))
        mdl_style = "magenta" if "opus" in mdl else ("cyan" if "sonnet" in mdl else "dim")
        src_color = "yellow" if ("/" in source or source == "paperclip") else ("green" if source == "cli" else ("cyan" if "atlas" in source else "dim"))

        # Derive project for active session from ledger files
        project = "—"
        # Check if we have files in ledger to derive project
        ledger_files = []
        for e in entries:
            if e.get("session") == sid:
                snippet = e.get("tool_snippet", "")
                if snippet:
                    ledger_files.append(snippet)
        # Simple heuristic from directive or source
        if source in ("atlas-be", "atlas-fe"):
            project = "atlas"
        elif source == "openclaw":
            project = "openclaw"
        elif source == "frank":
            project = "frank"
        elif "/" in source:
            project = source.split("/")[0].lower()
        else:
            # Try to infer from directive text
            d_lower = directive.lower() if directive else ""
            for p in ("claude-watch", "atlas", "paperclip", "openclaw", "frank"):
                if p in d_lower:
                    project = p
                    break

        t.add_row(
            f"[dim]{start_str}[/dim]",
            f"[cyan]{sid}[/cyan]",
            f"[{src_color}]{source}[/{src_color}]",
            f"[dim]{project}[/dim]",
            f"[{mdl_style}]{mdl}[/{mdl_style}]",
            f"[dim]{age}[/dim]",
            f"[{color}]{delta}[/{color}]",
            directive,
        )

        # ── Sub-row: live call state ──
        cpu = _get_pid_cpu(pid)
        lc = last_call.get(sid)
        if lc:
            secs_since = int((now_utc - lc[0]).total_seconds())
            tool_name = lc[1]
            token_delta = lc[2] - first_out.get(sid, 0)
        else:
            secs_since = None
            tool_name = "?"
            token_delta = 0

        # State detection
        if secs_since is not None and secs_since < 15:
            state = f"[bold green]>> {tool_name[:12]}[/bold green]"
        elif cpu > 20:
            state = "[bold yellow]thinking...[/bold yellow]"
        elif secs_since is not None and secs_since < 120:
            state = f"[dim]~ {tool_name[:12]}[/dim]"
        else:
            state = "[dim]idle[/dim]"

        # Elapsed
        if secs_since is not None:
            m, s = divmod(secs_since, 60)
            elapsed_str = f"{m}m{s:02d}s" if m else f"{s}s"
        else:
            elapsed_str = "—"

        # Tokens
        tok_str = f"{token_delta / 1000:.1f}k" if token_delta >= 1000 else str(token_delta)

        # CPU
        cpu_str = f"{cpu:.0f}%"
        cpu_style = "bold yellow" if cpu > 50 else ("dim" if cpu < 5 else "")
        cpu_val = f"[{cpu_style}]{cpu_str}[/{cpu_style}]" if cpu_style else cpu_str

        t.add_row(
            "", "", "", "",
            f"  {state}",
            f"[dim]ago:[/dim] {elapsed_str}",
            f"[dim]tok:[/dim] {tok_str}",
            f"[dim]cpu:[/dim] {cpu_val}",
        )


    return Panel(t, title=title, border_style="cyan")


def _get_pid_cpu(pid):
    """Get CPU usage percentage for a PID."""
    try:
        r = subprocess.run(
            ['ps', '-p', str(pid), '-o', '%cpu='],
            capture_output=True, text=True, timeout=2,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def make_tool_stats():
    entries = _load_ledger(last_n=500)
    tool_events = [e for e in entries if e.get("type") == "tool_use"]
    counts = defaultdict(int)
    for e in tool_events:
        counts[e.get("tool", "unknown")] += 1
    t = Table(show_header=True, header_style="bold green", box=None, padding=(0, 1), expand=True)
    t.add_column("Tool", overflow="ellipsis", no_wrap=True, ratio=4)
    t.add_column("Calls", min_width=5, justify="right", no_wrap=True)
    for tool, count in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:12]:
        t.add_row(_shorten_tool(tool), str(count))
    return Panel(t, title="[bold]Tool Frequency[/bold]  [dim](last 500 events)[/dim]", border_style="green")


def make_drain_panel():
    entries = _load_ledger(last_n=200)
    drain_events = [e for e in entries if e.get("type") == "tool_drain" and e.get("delta_5h", 0) > 0][-12:]
    status_color, status_msg = _drain_status(drain_events)

    t = Table(show_header=True, header_style="bold yellow", box=None, padding=(0, 1), expand=True)
    t.add_column("Time", min_width=8, no_wrap=True)
    t.add_column("Delta", min_width=6, no_wrap=True)
    t.add_column("Burn/min", min_width=8, no_wrap=True)
    t.add_column("Sessions", min_width=4, no_wrap=True)
    t.add_column("Desktop", min_width=7, no_wrap=True)

    t.add_row(f"[{status_color}]{status_msg}[/{status_color}]", "", "", "", "")

    if not drain_events:
        t.add_row("[dim green]no drain events recorded[/dim green]", "", "", "", "")
    else:
        for e in reversed(drain_events):
            ts = e.get("ts", "")
            try:
                ts_str = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
            except Exception:
                ts_str = "?"
            delta = e.get("delta_5h", 0)
            burn = e.get("burn_rate_per_min", 0)
            sessions = e.get("cli_sessions", "?")
            desktop = "YES" if e.get("desktop") else "no"
            burn_color = "red" if float(burn) > 1 else "yellow"
            t.add_row(
                f"[dim]{ts_str}[/dim]", f"[red]+{delta}%[/red]",
                f"[{burn_color}]{burn:.2f}%[/{burn_color}]", str(sessions),
                f"[bold red]{desktop}[/bold red]" if desktop == "YES" else f"[dim]{desktop}[/dim]",
            )
    return Panel(t, title="[bold]Passive Drain[/bold]  [dim](non-zero only)[/dim]", border_style="yellow")


# ── session tasks (Supabase) ────────────────────────────────────────────────

def _get_session_tasks(session_id=None, today_only=True):
    """Fetch session tasks from Supabase.

    Args:
        session_id: Filter to specific session (e.g. 'cc-12345'). None = all.
        today_only: If True and no session_id, only fetch today's tasks.

    Returns: list of dicts with keys:
        id, session_id, working_session, task_name, project, status,
        started_at, completed_at, artifacts, notes, created_at
    """
    import urllib.request
    import json as _json

    url = "https://zoirudjyqfqvpxsrxepr.supabase.co/rest/v1/session_tasks"
    params = ["order=created_at.desc", "limit=100"]
    if session_id:
        params.append(f"session_id=eq.{session_id}")
    elif today_only:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        params.append(f"created_at=gte.{today}")

    full_url = url + "?" + "&".join(params)
    key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvaXJ1ZGp5cWZxdnB4c3J4ZXByIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjgwMzE4MjgsImV4cCI6MjA4MzYwNzgyOH0.6W6OzRfJ-nmKN_23z1OBCS4Cr-ODRq9DJmF_yMwOCfo"

    req = urllib.request.Request(full_url, headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception:
        return []


def _get_project_tasks(project=None):
    """Fetch project tasks from Supabase.

    Args:
        project: Filter to specific project. None = all.

    Returns: list of dicts with keys:
        id, project, task_name, phase, status, build_order, claimed_by,
        route, file_path, notes, notion_ref, figma_ref, created_at, updated_at
    """
    import urllib.request
    import json as _json

    url = "https://zoirudjyqfqvpxsrxepr.supabase.co/rest/v1/project_tasks"
    params = ["order=build_order.asc.nullslast,created_at.desc", "limit=200"]
    if project:
        params.append(f"project=eq.{project}")

    full_url = url + "?" + "&".join(params)
    key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvaXJ1ZGp5cWZxdnB4c3J4ZXByIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjgwMzE4MjgsImV4cCI6MjA4MzYwNzgyOH0.6W6OzRfJ-nmKN_23z1OBCS4Cr-ODRq9DJmF_yMwOCfo"

    req = urllib.request.Request(full_url, headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception:
        return []  # end of _get_project_tasks


# ── Window Scoring (Gamification) ────────────────────────────────────────────

WINDOW_SCORES_FILE = Path.home() / ".claude/logs/window-scores.jsonl"


def _score_dimension(value, threshold):
    if threshold <= 0:
        return 5.0
    return round(min(value / threshold, 1.0) * 5.0, 1)


def _stars_display(score):
    full = int(score)
    half = (score - full) >= 0.25
    empty = 5 - full - (1 if half else 0)
    return "★" * full + ("½" if half else "") + "☆" * empty


def _score_window(window_start_ts, window_reset_ts):
    with _index_lock:
        snapshot = _index_cache
    entries = _load_ledger()
    window_entries = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
            if window_start_ts <= ts <= window_reset_ts:
                window_entries.append(e)
        except Exception:
            pass
    if not window_entries:
        return None

    last_five = 0
    for e in reversed(window_entries):
        if e.get("five_pct") is not None:
            last_five = float(e["five_pct"])
            break
    burn_score = _score_dimension(last_five, 95.0)

    max_parallel = 0
    for e in window_entries:
        if e.get("type") == "tool_drain" and e.get("cli_sessions", 0) > max_parallel:
            max_parallel = e["cli_sessions"]
    para_score = _score_dimension(max_parallel, 4)

    _load_index()
    total_commits = 0
    window_projects = set()
    for sid, entry in snapshot.items():
        try:
            lts = entry.get("last_ts", "")
            if not lts:
                continue
            sts = datetime.fromisoformat(lts.replace("Z", "+00:00"))
            if window_start_ts <= sts <= window_reset_ts + timedelta(minutes=30):
                total_commits += len(entry.get("accomplishments", {}).get("git_commits", []))
                proj = entry.get("project", "")
                if proj and proj != "\u2014":
                    window_projects.add(proj)
        except Exception:
            pass
    for e in window_entries:
        if e.get("type") == "tool_use":
            d = (e.get("directive") or "").lower()
            for p in ("atlas", "claude-watch", "paperclip", "openclaw", "frank", "kaa"):
                if p in d:
                    window_projects.add(p)

    # Augment with cycle monitor items
    ci_done, ci_projects = _get_cycle_items_for_scoring(window_start_ts.isoformat())
    total_commits += ci_done
    window_projects |= ci_projects

    ship_score = _score_dimension(total_commits, 5)
    breadth_score = _score_dimension(len(window_projects), 4)

    drain_rates = []
    drain_ts = []
    for e in window_entries:
        if e.get("type") == "tool_drain":
            r = e.get("burn_rate_per_min", 0)
            if r > 0:
                drain_rates.append(r)
            try:
                drain_ts.append(datetime.fromisoformat(e["ts"].replace("Z", "+00:00")))
            except Exception:
                pass
    avg_rate = sum(drain_rates) / len(drain_rates) if drain_rates else 0
    idle_gaps = 0
    drain_ts.sort()
    for i in range(1, len(drain_ts)):
        if (drain_ts[i] - drain_ts[i - 1]).total_seconds() > 600:
            idle_gaps += 1
    rate_score = _score_dimension(avg_rate, 1.0)
    vel_score = max(0.0, round(rate_score - min(idle_gaps * 0.5, 3.0), 1))

    overall = round(
        burn_score * 0.30 + para_score * 0.20 + ship_score * 0.20
        + breadth_score * 0.15 + vel_score * 0.15, 1)
    overall = round(overall * 2) / 2

    return {
        "window_start": window_start_ts.isoformat(),
        "window_reset": window_reset_ts.isoformat(),
        "burn": burn_score, "parallelism": para_score,
        "shipping": ship_score, "breadth": breadth_score,
        "velocity": vel_score, "overall": overall,
        "stars": _stars_display(overall),
        "burn_pct": last_five, "max_parallel": max_parallel,
        "commits": total_commits, "projects": len(window_projects),
        "avg_rate": round(avg_rate, 2),
    }


BATTLESTATION_FILE = Path.home() / ".claude/battlestation.json"
_SUPABASE_URL = "https://zoirudjyqfqvpxsrxepr.supabase.co/rest/v1"
_SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvaXJ1ZGp5cWZxdnB4c3J4ZXByIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjgwMzE4MjgsImV4cCI6MjA4MzYwNzgyOH0.6W6OzRfJ-nmKN_23z1OBCS4Cr-ODRq9DJmF_yMwOCfo"


def _get_battlestation_config():
    try:
        if BATTLESTATION_FILE.exists():
            with open(BATTLESTATION_FILE) as f:
                return json.loads(f.read())
    except Exception:
        pass
    return {"user_id": "unknown", "display_name": "Unknown", "team": ""}


def _post_score_to_supabase(score):
    """POST a window score to the shared Supabase leaderboard."""
    import urllib.request
    config = _get_battlestation_config()
    payload = {
        "user_id": config["user_id"],
        "user_display": config.get("display_name", config["user_id"]),
        "window_start": score["window_start"],
        "window_reset": score["window_reset"],
        "burn": score.get("burn", 0),
        "parallelism": score.get("parallelism", 0),
        "shipping": score.get("shipping", 0),
        "breadth": score.get("breadth", 0),
        "velocity": score.get("velocity", 0),
        "overall": score.get("overall", 0),
        "stars": score.get("stars", ""),
        "burn_pct": score.get("burn_pct", 0),
        "max_parallel": score.get("max_parallel", 0),
        "commits": score.get("commits", 0),
        "projects": score.get("projects", 0),
        "avg_rate": score.get("avg_rate", 0),
        "streak": score.get("streak", 0),
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_SUPABASE_URL}/window_scores",
            data=data,
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _save_window_score(score):
    if not score:
        return
    try:
        WINDOW_SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(WINDOW_SCORES_FILE, "a") as f:
            f.write(json.dumps(score) + "\n")
    except Exception:
        pass
    # Also publish to shared leaderboard
    _post_score_to_supabase(score)


def _get_window_scores(limit=20):
    if not WINDOW_SCORES_FILE.exists():
        return []
    scores = []
    try:
        with open(WINDOW_SCORES_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        scores.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    scores.reverse()
    return scores[:limit]


def _get_streak(scores=None):
    if scores is None:
        scores = _get_window_scores()
    streak = 0
    for s in scores:
        if s.get("overall", 0) >= 4.0:
            streak += 1
        else:
            break
    return streak


_last_scored_window = None


def _check_and_score_completed_window():
    global _last_scored_window
    try:
        data = _get_burndown_data()
        if not data:
            return None
        window_start = data["window_start"]
        window_key = window_start.isoformat()
        if _last_scored_window == window_key:
            return None
        _last_scored_window = window_key

        existing = _get_window_scores(limit=5)
        for s in existing:
            if s.get("window_start") == window_key:
                return None

        prev_reset = window_start
        prev_start = prev_reset - timedelta(hours=5)
        score = _score_window(prev_start, prev_reset)
        if score and score.get("burn_pct", 0) > 1:
            streak = _get_streak(existing)
            score["streak"] = (streak + 1) if score["overall"] >= 4.0 else 0
            _save_window_score(score)
            return score
    except Exception:
        pass
    return None


def _get_leaderboard(days=7):
    """Fetch leaderboard from Supabase, aggregated by user."""
    import urllib.request
    import json as _json
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"{_SUPABASE_URL}/window_scores"
        f"?window_start=gte.{cutoff}"
        f"&order=created_at.desc&limit=500"
    )
    try:
        req = urllib.request.Request(url, headers={
            "apikey": _SUPABASE_KEY,
            "Authorization": f"Bearer {_SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = _json.loads(resp.read())
    except Exception:
        return []

    # Aggregate by user
    users = {}
    for r in rows:
        uid = r.get("user_id", "?")
        if uid not in users:
            users[uid] = {
                "user_id": uid,
                "display_name": r.get("user_display", uid),
                "scores": [],
            }
        users[uid]["scores"].append(r)

    leaderboard = []
    for uid, u in users.items():
        scores = u["scores"]
        n = len(scores)
        avg_overall = sum(s.get("overall", 0) for s in scores) / n if n else 0
        avg_burn = sum(s.get("burn", 0) for s in scores) / n if n else 0
        avg_ship = sum(s.get("shipping", 0) for s in scores) / n if n else 0
        avg_vel = sum(s.get("velocity", 0) for s in scores) / n if n else 0
        best = max((s.get("overall", 0) for s in scores), default=0)
        best_stars = _stars_display(best)
        # Current streak = streak of most recent score
        latest = max(scores, key=lambda s: s.get("window_start", ""))
        streak = latest.get("streak", 0)

        leaderboard.append({
            "user_id": uid,
            "display_name": u["display_name"],
            "windows": n,
            "avg_overall": round(avg_overall, 1),
            "avg_stars": _stars_display(round(avg_overall * 2) / 2),
            "best": best,
            "best_stars": best_stars,
            "avg_burn": round(avg_burn, 1),
            "avg_ship": round(avg_ship, 1),
            "avg_velocity": round(avg_vel, 1),
            "streak": streak,
        })

    leaderboard.sort(key=lambda x: x["avg_overall"], reverse=True)
    return leaderboard


# ── Cycles (5h Window Analytics + Planning) ─────────────────────────────────

CYCLE_PLANS_FILE = Path.home() / ".claude/logs/cycle-plans.jsonl"

_cycles_cache = None  # type: Optional[List[dict]]
_cycles_cache_ts = 0.0


def _get_cycle_boundaries(limit=20):
    # type: (int) -> List[Tuple[datetime, datetime]]
    """Return list of (start, end) datetime pairs for detected cycles, newest first."""
    boundaries = []  # type: List[Tuple[datetime, datetime, bool]]
    # bool = authoritative (from window-scores)

    # 1. Window-scores entries (authoritative)
    for ws in _get_window_scores(limit=50):
        try:
            start = datetime.fromisoformat(ws["window_start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(ws["window_reset"].replace("Z", "+00:00"))
            boundaries.append((start, end, True))
        except Exception:
            pass

    # 2. Current cycle from live rate-limit data
    try:
        five, _seven, five_reset_ts, _seven_reset_ts = _current_pct()
        if five_reset_ts and five != "?":
            if isinstance(five_reset_ts, str) and five_reset_ts:
                end_dt = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
            elif isinstance(five_reset_ts, (int, float)):
                end_dt = datetime.fromtimestamp(five_reset_ts, tz=timezone.utc)
            else:
                end_dt = None
            if end_dt:
                start_dt = end_dt - timedelta(hours=5)
                boundaries.append((start_dt, end_dt, False))
    except Exception:
        pass

    # 3. Gap-fill from ledger: detect five_pct resets
    try:
        ledger = _load_ledger()
        prev_pct = None
        for entry in ledger:
            cur_pct = entry.get("five_pct")
            if cur_pct is None:
                continue
            try:
                cur_pct = float(cur_pct)
            except (ValueError, TypeError):
                continue
            if prev_pct is not None and prev_pct > 15 and cur_pct < 5:
                # Reset detected — this entry starts a new cycle
                try:
                    ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                    cycle_start = ts
                    cycle_end = ts + timedelta(hours=5)
                    boundaries.append((cycle_start, cycle_end, False))
                except Exception:
                    pass
            prev_pct = cur_pct
    except Exception:
        pass

    # 4. Deduplicate: if two overlap within 30 min, keep authoritative
    deduped = []  # type: List[Tuple[datetime, datetime]]
    # Sort by start time
    boundaries.sort(key=lambda x: x[0])
    for start, end, auth in boundaries:
        merged = False
        for i, (es, ee) in enumerate(deduped):
            # Check overlap within 30 min of start
            if abs((start - es).total_seconds()) < 1800:
                # Keep existing if authoritative already captured, or replace with authoritative
                if auth:
                    deduped[i] = (start, end)
                merged = True
                break
        if not merged:
            deduped.append((start, end))

    # 5. Sort newest first, limit
    deduped.sort(key=lambda x: x[0], reverse=True)
    return deduped[:limit]


def _build_cycle_record(start_ts, end_ts, is_current=False):
    # type: (datetime, datetime, bool) -> dict
    """Build a full cycle record from boundaries."""
    cycle_id = start_ts.isoformat()

    # Filter sessions within this cycle
    all_sessions = _get_session_history()
    cycle_sessions = []
    for s in all_sessions:
        try:
            first = s["first_ts"]
            if not isinstance(first, datetime):
                first = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            if start_ts <= first < end_ts:
                cycle_sessions.append(s)
        except Exception:
            pass

    # Filter ledger entries within this cycle
    ledger = _load_ledger()
    cycle_ledger = []
    for entry in ledger:
        try:
            ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
            if start_ts <= ts <= end_ts:
                cycle_ledger.append(entry)
        except Exception:
            pass

    # Peak five_pct
    peak_five = 0
    for entry in cycle_ledger:
        try:
            pct = float(entry.get("five_pct", 0))
            if pct > peak_five:
                peak_five = pct
        except (ValueError, TypeError):
            pass

    # Token sum and cost
    total_tokens = 0
    total_cost = 0.0
    for s in cycle_sessions:
        tok = s.get("output_tokens", 0) or 0
        total_tokens += tok
        model = s.get("model", "")
        total_cost += _estimate_cost(tok, model)

    # Aggregate accomplishments
    merged_acc = {
        "files_edited": [],
        "files_created": [],
        "git_commits": [],
        "git_pushes": [],
        "skills": [],
        "mcp_ops": [],
        "bash_notable": [],
        "user_prompts": [],
        "errors": 0,
        "turn_count": 0,
    }
    for s in cycle_sessions:
        try:
            acc = _extract_accomplishments(s["session_id"])
            if not acc:
                continue
            for key in ("files_edited", "files_created", "git_commits",
                        "git_pushes", "bash_notable", "user_prompts"):
                merged_acc[key].extend(acc.get(key, []))
            for key in ("mcp_ops", "skills"):
                # Union
                existing = set(merged_acc[key])
                for item in acc.get(key, []):
                    if item not in existing:
                        merged_acc[key].append(item)
                        existing.add(item)
            merged_acc["errors"] += acc.get("errors", 0)
            merged_acc["turn_count"] += acc.get("turn_count", 0)
        except Exception:
            pass

    # Window score lookup
    window_score = None
    for ws in _get_window_scores(limit=50):
        try:
            ws_start = datetime.fromisoformat(ws["window_start"].replace("Z", "+00:00"))
            if abs((ws_start - start_ts).total_seconds()) < 1800:
                window_score = ws
                break
        except Exception:
            pass

    # Gravity label
    gravity_label = _gravity_center(merged_acc, fallback="")

    return {
        "cycle_id": cycle_id,
        "start_ts": start_ts.isoformat(),
        "end_ts": end_ts.isoformat(),
        "is_current": is_current,
        "session_count": len(cycle_sessions),
        "peak_five_pct": peak_five,
        "total_output_tokens": total_tokens,
        "total_cost": total_cost,
        "cost_str": _format_cost(total_cost),
        "accomplishments": merged_acc,
        "gravity_label": gravity_label,
        "window_score": window_score,
        "stars": _stars_display(window_score["overall"]) if window_score else "",
        "overall_score": window_score.get("overall", 0) if window_score else 0,
        "sessions": [s["session_id"] for s in cycle_sessions],
    }


def _get_all_cycles(limit=20):
    # type: (int) -> List[dict]
    """Get all cycle records with 30s cache TTL."""
    global _cycles_cache, _cycles_cache_ts
    now = time.time()
    if _cycles_cache is not None and (now - _cycles_cache_ts) < 30:
        return _cycles_cache[:limit]

    boundaries = _get_cycle_boundaries(limit=limit)
    if not boundaries:
        _cycles_cache = []
        _cycles_cache_ts = now
        return []

    # Determine which is the current cycle
    current_end = None
    try:
        _five, _seven, five_reset_ts, _seven_reset_ts = _current_pct()
        if five_reset_ts:
            if isinstance(five_reset_ts, str) and five_reset_ts:
                current_end = datetime.fromisoformat(five_reset_ts.replace("Z", "+00:00"))
            elif isinstance(five_reset_ts, (int, float)):
                current_end = datetime.fromtimestamp(five_reset_ts, tz=timezone.utc)
    except Exception:
        pass

    cycles = []
    for start, end in boundaries:
        is_current = False
        if current_end and abs((end - current_end).total_seconds()) < 1800:
            is_current = True
        try:
            record = _build_cycle_record(start, end, is_current=is_current)
            cycles.append(record)
        except Exception:
            pass

    _cycles_cache = cycles
    _cycles_cache_ts = now
    return cycles[:limit]


def _get_current_cycle():
    # type: () -> Optional[dict]
    """Return the current (is_current=True) cycle, or None."""
    for c in _get_all_cycles():
        if c.get("is_current"):
            return c
    return None


def _get_cycle_sessions(cycle_id):
    # type: (str) -> List[dict]
    """Return full session history entries for sessions within a cycle."""
    # Find matching cycle boundaries
    boundaries = _get_cycle_boundaries()
    target_start = None
    target_end = None
    for start, end in boundaries:
        if start.isoformat() == cycle_id:
            target_start = start
            target_end = end
            break

    if target_start is None or target_end is None:
        return []

    all_sessions = _get_session_history()
    result = []
    for s in all_sessions:
        try:
            first = s["first_ts"]
            if not isinstance(first, datetime):
                first = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            if target_start <= first < target_end:
                result.append(s)
        except Exception:
            pass
    return result


# ── Cycle Planning ───────────────────────────────────────────────────────────

def _load_cycle_plans():
    # type: () -> Dict[str, dict]
    """Read CYCLE_PLANS_FILE. Return dict keyed by cycle_id, last entry wins."""
    if not CYCLE_PLANS_FILE.exists():
        return {}
    plans = {}  # type: Dict[str, dict]
    try:
        with open(CYCLE_PLANS_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        cid = entry.get("cycle_id")
                        if cid:
                            plans[cid] = entry
                    except Exception:
                        pass
    except Exception:
        pass
    return plans


def _get_cycle_plan(cycle_id):
    # type: (str) -> Optional[dict]
    """Get plan for a specific cycle."""
    plans = _load_cycle_plans()
    return plans.get(cycle_id)


def _save_cycle_plan(plan):
    # type: (dict) -> None
    """Append plan to CYCLE_PLANS_FILE with updated_at timestamp."""
    plan["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        CYCLE_PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CYCLE_PLANS_FILE, "a") as f:
            f.write(json.dumps(plan) + "\n")
    except Exception:
        pass


def _get_plannable_tasks():
    # type: () -> List[dict]
    """Get tasks ready for cycle planning, enriched with est_pct."""
    tasks = _get_project_tasks()
    ready = [t for t in tasks if t.get("status") == "ready"]
    # Sort by priority (nulls last), then build_order (nulls last)
    def _sort_key(t):
        pri = t.get("priority")
        bo = t.get("build_order")
        return (
            pri if pri is not None else 9999,
            bo if bo is not None else 9999,
        )
    ready.sort(key=_sort_key)
    # Enrich with est_pct
    for t in ready:
        tok_k = t.get("est_tokens_k")
        if tok_k is not None:
            try:
                t["est_pct"] = _estimate_pct_for_tokens(float(tok_k))
            except (ValueError, TypeError):
                t["est_pct"] = 0.0
        else:
            t["est_pct"] = 0.0
    return ready


def _estimate_pct_for_tokens(tokens_k):
    # type: (float) -> float
    """Convert estimated tokens (thousands) to estimated % of 5h window.

    Baseline: ~5500 output tokens ~ 1% of 5h window.
    """
    pct = tokens_k * 1000 / 5500
    return round(pct, 1)


# -- Cycle Monitor (Supabase-backed freeform items per 5h window) -----------


def _get_cycle_items(window_start):
    # type: (str) -> List[dict]
    """GET cycle_items for a given window_start."""
    import urllib.request
    config = _get_battlestation_config()
    url = (
        f"{_SUPABASE_URL}/cycle_items"
        f"?user_id=eq.{config['user_id']}"
        f"&window_start=eq.{window_start}"
        f"&order=created_at.asc"
    )
    try:
        req = urllib.request.Request(url, headers={
            "apikey": _SUPABASE_KEY,
            "Authorization": f"Bearer {_SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def _post_cycle_item(window_start, category, title, project=""):
    # type: (str, str, str, str) -> Optional[dict]
    """POST a new cycle_item. Returns the inserted row or None."""
    import urllib.request
    config = _get_battlestation_config()
    payload = {
        "user_id": config["user_id"],
        "window_start": window_start,
        "category": category,
        "title": title,
        "status": "open",
        "project": project,
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_SUPABASE_URL}/cycle_items",
            data=data,
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read())
            return rows[0] if rows else None
    except Exception:
        return None


def _update_cycle_item(item_id, updates):
    # type: (str, dict) -> bool
    """PATCH a cycle_item by id. Returns True on success."""
    import urllib.request
    try:
        data = json.dumps(updates).encode()
        req = urllib.request.Request(
            f"{_SUPABASE_URL}/cycle_items?id=eq.{item_id}",
            data=data,
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _delete_cycle_item(item_id):
    # type: (str) -> bool
    """DELETE a cycle_item by id. Returns True on success."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{_SUPABASE_URL}/cycle_items?id=eq.{item_id}",
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
            },
            method="DELETE",
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _get_recent_cycle_summaries(limit=3):
    # type: (int) -> List[dict]
    """Summarise recent completed cycles with item counts."""
    cycles = _get_all_cycles()
    summaries = []
    for c in cycles:
        if c.get("is_current"):
            continue
        items = _get_cycle_items(c["cycle_id"])
        items_done = sum(1 for i in items if i.get("status") == "done")
        items_rolled = sum(1 for i in items if i.get("status") == "rolled")
        projects = list({i.get("project", "") for i in items if i.get("project")})
        try:
            dt = datetime.fromisoformat(c["cycle_id"].replace("Z", "+00:00"))
            when_str = dt.astimezone().strftime(f"%b {dt.day} %-I%p").replace("AM", "am").replace("PM", "pm")
        except Exception:
            when_str = c["cycle_id"][:16]
        summaries.append({
            "window_start": c["cycle_id"],
            "stars": c.get("stars", ""),
            "items_total": len(items),
            "items_done": items_done,
            "items_rolled": items_rolled,
            "projects": projects,
            "when_str": when_str,
        })
        if len(summaries) >= limit:
            break
    return summaries


def _get_cycle_items_for_scoring(window_start):
    # type: (str) -> Tuple[int, set]
    """Return (done_count, project_set) for scoring integration."""
    items = _get_cycle_items(window_start)
    done_count = sum(1 for i in items if i.get("status") == "done")
    projects = {i.get("project", "") for i in items if i.get("project")}
    return done_count, projects


def _roll_cycle_items(old_window_start, new_window_start):
    # type: (str, str) -> int
    """Roll open items from old window to new window. Returns count rolled."""
    import urllib.request
    config = _get_battlestation_config()
    # Fetch open items from old window
    url = (
        f"{_SUPABASE_URL}/cycle_items"
        f"?user_id=eq.{config['user_id']}"
        f"&window_start=eq.{old_window_start}"
        f"&status=eq.open"
        f"&order=created_at.asc"
    )
    try:
        req = urllib.request.Request(url, headers={
            "apikey": _SUPABASE_KEY,
            "Authorization": f"Bearer {_SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            open_items = json.loads(resp.read())
    except Exception:
        return 0

    rolled = 0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for item in open_items:
        # Clone to new window
        clone_payload = {
            "user_id": config["user_id"],
            "window_start": new_window_start,
            "category": item.get("category", ""),
            "title": item.get("title", ""),
            "status": "open",
            "project": item.get("project", ""),
        }
        try:
            data = json.dumps(clone_payload).encode()
            req = urllib.request.Request(
                f"{_SUPABASE_URL}/cycle_items",
                data=data,
                headers={
                    "apikey": _SUPABASE_KEY,
                    "Authorization": f"Bearer {_SUPABASE_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            continue

        # Mark original as rolled
        try:
            patch_data = json.dumps({"status": "rolled", "resolved_at": now_iso}).encode()
            req = urllib.request.Request(
                f"{_SUPABASE_URL}/cycle_items?id=eq.{item['id']}",
                data=patch_data,
                headers={
                    "apikey": _SUPABASE_KEY,
                    "Authorization": f"Bearer {_SUPABASE_KEY}",
                    "Content-Type": "application/json",
                },
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=5)
            rolled += 1
        except Exception:
            pass

    return rolled


# ── Test Queue ────────────────────────────────────────────────────────────────

def _get_test_queue(project=None, status="pending"):
    # type: (str, str) -> list
    """Fetch test_queue items from Supabase."""
    import urllib.request
    from urllib.parse import quote
    config = _get_battlestation_config()
    url = (
        f"{_SUPABASE_URL}/test_queue"
        f"?user_id=eq.{config['user_id']}"
    )
    if status is not None:
        url += f"&status=eq.{quote(status)}"
    if project:
        url += f"&project=eq.{quote(project)}"
    url += "&order=created_at.desc&limit=200"
    try:
        req = urllib.request.Request(url, headers={
            "apikey": _SUPABASE_KEY,
            "Authorization": f"Bearer {_SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def _add_test_item(title, project="", source="manual", source_ref="", route="", priority="normal"):
    # type: (str, str, str, str, str, str) -> dict
    """Insert a new test_queue item. Returns inserted row or empty dict."""
    import urllib.request
    config = _get_battlestation_config()
    payload = {
        "user_id": config["user_id"],
        "title": title[:200],
        "project": project,
        "source": source,
        "source_ref": source_ref,
        "route": route,
        "priority": priority,
        "status": "pending",
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_SUPABASE_URL}/test_queue",
            data=data,
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read())
            return rows[0] if rows else {}
    except Exception:
        return {}


def _update_test_item(item_id, status, notes=""):
    # type: (str, str, str) -> bool
    """Update test_queue item status. Sets tested_at on pass/fail/skip."""
    import urllib.request
    updates = {"status": status, "notes": notes}
    if status in ("pass", "fail", "skip"):
        updates["tested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = json.dumps(updates).encode()
        req = urllib.request.Request(
            f"{_SUPABASE_URL}/test_queue?id=eq.{item_id}",
            data=data,
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _delete_test_item(item_id):
    # type: (str) -> bool
    """Delete a test_queue item by id."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{_SUPABASE_URL}/test_queue?id=eq.{item_id}",
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
            },
            method="DELETE",
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def _import_atlas_qa_tests():
    # type: () -> int
    """Parse Atlas QA test-definitions.ts and upsert pending items.
    Returns count of newly inserted items."""
    import re
    import os

    ts_path = os.path.expanduser("~/atlas-portal/src/app/admin/qa/test-definitions.ts")
    try:
        with open(ts_path) as fh:
            content = fh.read()
    except Exception:
        return 0

    section_route_map = {
        "auth": "/auth",
        "dash": "/dashboard",
        "craf": "/crafting",
        "voic": "/voices",
        "aler": "/signals",
        "sign": "/signals",
        "anal": "/analytics",
        "brie": "/briefing",
        "orac": "/onboarding",
        "camp": "/campaigns",
        "aren": "/arena",
        "mana": "/management",
        "team": "/management",
        "queu": "/queue",
        "nav": "/",
        "perf": "/",
        "desi": "/",
        "a11y": "/",
        "erro": "/",
    }

    priority_map = {
        "critical": "high",
        "high": "high",
        "medium": "normal",
        "normal": "normal",
        "low": "low",
    }

    # Get existing qa source_refs to avoid duplicates
    existing = _get_test_queue(status=None)
    existing_refs = {
        item["source_ref"] for item in existing
        if item.get("source") == "qa" and item.get("source_ref")
    }

    test_pattern = re.compile(
        r'id:\s*["\']([A-Z0-9]+-\d+)["\'].*?name:\s*["\']([^"\']+)["\']',
        re.DOTALL,
    )
    priority_pattern = re.compile(r'priority:\s*["\']([^"\']+)["\']')

    inserted = 0
    for match in test_pattern.finditer(content):
        test_id = match.group(1)
        test_name = match.group(2)

        if test_id in existing_refs:
            continue

        prefix = test_id.split("-")[0].lower()
        route = ""
        for key, r in section_route_map.items():
            if prefix.startswith(key):
                route = r
                break

        nearby = content[match.start():match.start() + 300]
        pri_match = priority_pattern.search(nearby)
        raw_priority = pri_match.group(1) if pri_match else "medium"
        priority = priority_map.get(raw_priority, "normal")

        result = _add_test_item(
            title=f"{test_id}: {test_name}",
            project="atlas",
            source="qa",
            source_ref=test_id,
            route=route,
            priority=priority,
        )
        if result:
            inserted += 1

    return inserted
