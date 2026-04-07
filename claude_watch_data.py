"""
claude-watch data layer — shared by Rich and Textual versions.
All data fetching, caching, and computation lives here.
"""

import json
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.panel import Panel
from rich.table import Table

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
    except Exception:
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
    except Exception:
        pass
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
        return f"{h}h{m:02d}m"
    except Exception:
        return "?"


def _reset_day(reset_ts):
    if not reset_ts:
        return "?"
    try:
        dt = datetime.fromisoformat(reset_ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime(f"%a %b {dt.day}")
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
                        delta = "reset"  # 5h window reset during session
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
                        except Exception:
                            pass
        except Exception:
            pass
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
    entry = _index_cache.get(session_id, {})
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
    s = user_input.strip()

    # Try as CCID number: "72887" → "cc-72887"
    if s.isdigit():
        s = f"cc-{s}"

    # Try as cc-PID
    if s.startswith("cc-"):
        uuid = _ccid_to_uuid.get(s)
        if uuid:
            return _index_cache.get(uuid)
        # Fallback: scan index
        for uid, entry in _index_cache.items():
            if entry.get("ccid") == s:
                return entry
        return None

    # Try as UUID prefix
    for uid, entry in _index_cache.items():
        if uid.startswith(s):
            return entry

    return None


# ── session index ────────────────────────────────────────────────────────────

_index_cache = {}
_index_loaded = False
_index_building = False
_index_thread = None


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
                    except Exception:
                        pass
        except Exception:
            pass
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
                except Exception:
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
    if _index_building:
        return
    _index_building = True
    try:
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
        if new_entries:
            # Rewrite full index (deduped by session_id) instead of appending
            with open(SESSION_INDEX, "w") as fh:
                for entry in known.values():
                    fh.write(json.dumps(entry) + "\n")
            _index_cache = dict(known)
            _rebuild_ccid_index()
    except Exception:
        pass
    finally:
        _index_building = False


def _ensure_index():
    global _index_thread
    if not _index_loaded:
        _load_index()
    if _index_thread is None or not _index_thread.is_alive():
        _index_thread = threading.Thread(target=_build_or_update_index, daemon=True)
        _index_thread.start()


def _get_session_history():
    _ensure_index()
    # Don't exclude any sessions — show everything in history.
    # The current session appears in both Active Sessions and Session History.
    # This is better than sessions mysteriously disappearing.
    current_session_id = None

    today = datetime.now(timezone.utc).astimezone().date()
    sessions = []

    for sid, entry in _index_cache.items():
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
                        pct_str = "reset"  # 5h window reset during session
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
    entry = _index_cache.get(session_id)
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    by_source = defaultdict(lambda: {"output_tokens": 0, "sessions": 0})
    total_output = 0

    for sid, entry in _index_cache.items():
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
    today = datetime.now().astimezone().date()
    result = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        total = sum(
            e.get("output_tokens", 0)
            for e in _index_cache.values()
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
    for entry in _index_cache.values():
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    counts = defaultdict(int)  # type: Dict[str, int]
    last_seen = {}  # type: Dict[str, str]

    for sid, entry in _index_cache.items():
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
    for uuid, entry in _index_cache.items():
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
                pct_str = "reset"
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
        idx_entry = _index_cache.get(sid, {})
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
        source = _index_cache.get(index_sid, {}).get("source", "cli")
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

    return Panel(t, title="[bold white]Token Monitor[/bold white]", border_style="bright_blue")


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

    if not sessions:
        t.add_row("", "[dim]—[/dim]", "", "", "", "", "", "[dim]no active sessions[/dim]")
        return Panel(t, title="[bold]Active Sessions[/bold]  [dim](live)[/dim]", border_style="cyan")

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


    return Panel(t, title="[bold]Active Sessions[/bold]  [dim](live)[/dim]", border_style="cyan")


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
