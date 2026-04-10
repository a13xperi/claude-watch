#!/usr/bin/env python3
"""
extract_session.py — Structured knowledge extraction from Claude Code session transcripts.

Two extraction modes:
  1. Metadata-only — cheap extraction from session index data (gravity, commits, directive)
  2. Transcript extraction — reads JSONL transcript files, trims them, extracts conversation
     text for LLM processing

Usage as module:
    from extract_session import extract_metadata_only, trim_transcript, search_transcripts

Usage standalone (self-test):
    python3 extract_session.py
"""

import json
import logging
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase connection
# ---------------------------------------------------------------------------
SUPABASE_URL = "https://zoirudjyqfqvpxsrxepr.supabase.co/rest/v1"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvaXJ1ZGp5cWZxdnB4c3J4ZXByIiwi"
    "cm9sZSI6ImFub24iLCJpYXQiOjE3NjgwMzE4MjgsImV4cCI6MjA4MzYwNzgyOH0."
    "6W6OzRfJ-nmKN_23z1OBCS4Cr-ODRq9DJmF_yMwOCfo"
)

_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# classify_item_type
# ---------------------------------------------------------------------------
def classify_item_type(text: str) -> str:
    """Classify a commit message or text snippet into a build_ledger item_type.

    Rules are evaluated in priority order; first match wins.
    All matching is case-insensitive.
    """
    low = text.lower()

    if any(kw in low for kw in ("fix", "bug", "patch", "resolve", "hotfix")):
        return "fix"
    if any(kw in low for kw in ("decision", "chose", "decided", "going with", "[decision]")):
        return "decision"
    if any(kw in low for kw in ("refactor", "cleanup", "rename", "lint")):
        return "chore"
    if any(kw in low for kw in ("test", "spec", "coverage")):
        return "test"
    if any(kw in low for kw in ("doc", "readme", "comment")):
        return "docs"
    if any(kw in low for kw in ("add", "implement", "ship", "build", "create", "new", "feature")):
        return "feature"
    return "feature"


# ---------------------------------------------------------------------------
# derive_project_company
# ---------------------------------------------------------------------------
def derive_project_company(entry: dict) -> tuple:
    """Derive (project, company) from a session index entry.

    Inspects ``project_dir`` and ``project`` fields for known keywords.

    Returns:
        (project: str, company: str)
    """
    haystack = " ".join([
        entry.get("project_dir", ""),
        entry.get("project", ""),
    ]).lower()

    if "atlas-portal" in haystack or "atlas-backend" in haystack:
        return ("atlas", "delphi")
    if "paperclip" in haystack:
        return ("paperclip", "personal")
    if "token-watch" in haystack or "battlestation" in haystack or "claude-watch" in haystack:
        return ("token-watch", "personal")
    if "kaa" in haystack:
        return ("kaa", "kaa-landscape")
    if "frank" in haystack or "cdpc" in haystack:
        return ("frank-pilot", "frank-pilot")
    return (entry.get("project", "general"), "personal")


# ---------------------------------------------------------------------------
# extract_metadata_only
# ---------------------------------------------------------------------------
def extract_metadata_only(entry: dict) -> list:
    """Extract build_ledger items from session index metadata alone.

    Creates one item per git commit, plus an optional summary item derived
    from the ``gravity`` field when it contains meaningful content.

    Returns:
        List of dicts ready for Supabase insert into ``build_ledger``.
    """
    project, company = derive_project_company(entry)
    session_id = entry.get("session_id", "")
    slug = entry.get("slug", "")
    directive = entry.get("directive", "")
    first_ts = entry.get("first_ts", "")
    accomplishments = entry.get("accomplishments", {})

    base_notes = f"Directive: {directive}. Recovered from session {slug} ({first_ts})"

    items: list[dict] = []

    # One item per git commit
    for commit_msg in accomplishments.get("git_commits", []):
        items.append({
            "session_id": session_id,
            "project": project,
            "company": company,
            "item_type": classify_item_type(commit_msg),
            "title": commit_msg[:500],
            "source": "recovery",
            "test_status": "untested",
            "notes": base_notes,
        })

    # Summary item from gravity (if non-trivial)
    gravity = entry.get("gravity", "")
    if gravity and not re.match(r"^session\s*\(\d+\s+turns?\)$", gravity, re.IGNORECASE):
        items.append({
            "session_id": session_id,
            "project": project,
            "company": company,
            "item_type": "feature",
            "title": gravity[:500],
            "source": "recovery",
            "test_status": "untested",
            "notes": base_notes,
        })

    return items


# ---------------------------------------------------------------------------
# trim_transcript
# ---------------------------------------------------------------------------
def trim_transcript(jsonl_path: str, max_chars: int = 100_000) -> str:
    """Read a transcript JSONL file and extract conversation text.

    Filters to human and assistant messages only (skips tool_result and system).
    For assistant messages, only text blocks are kept (tool_use blocks are dropped).

    If the total length exceeds *max_chars*, the middle is trimmed and replaced
    with a marker.

    Returns:
        A single formatted string of the conversation.
    """
    parts: list[str] = []

    try:
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")
                ts = obj.get("timestamp", "")

                if msg_type == "human":
                    content = _extract_human_text(obj)
                    if content:
                        parts.append(f"[USER @ {ts}]\n{content}\n")

                elif msg_type == "assistant":
                    content = _extract_assistant_text(obj)
                    if content:
                        parts.append(f"[ASSISTANT @ {ts}]\n{content}\n")
                # skip tool_result, system, and anything else

    except (OSError, IOError) as exc:
        logger.warning("Failed to read transcript %s: %s", jsonl_path, exc)
        return ""

    full = "\n".join(parts)

    if len(full) > max_chars:
        third = max_chars // 3
        full = (
            full[:third]
            + "\n...\n[TRIMMED \u2014 middle omitted]\n...\n"
            + full[-third:]
        )

    return full


def _extract_human_text(obj: dict) -> str:
    """Extract text from a human-type transcript entry."""
    msg = obj.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def _extract_assistant_text(obj: dict) -> str:
    """Extract only text blocks from an assistant-type transcript entry."""
    msg = obj.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)
    return ""


# ---------------------------------------------------------------------------
# extract_unfinished_work
# ---------------------------------------------------------------------------
def extract_unfinished_work(entry: dict, transcript_text: str = None) -> list:
    """Identify unfinished work from a session.

    Heuristics:
    - Session has no close log (SL-*.md) -> likely killed mid-work
    - High error count + high turn count -> died mid-task
    - Last user messages hint at in-progress work

    Returns:
        List of project_tasks-ready dicts.
    """
    session_id = entry.get("session_id", "")
    first_ts = entry.get("first_ts", "")
    directive = entry.get("directive", "")
    accomplishments = entry.get("accomplishments", {})
    errors = accomplishments.get("errors", 0)
    turn_count = accomplishments.get("turn_count", 0)
    project, company = derive_project_company(entry)

    tasks: list[dict] = []

    # Check if this looks like a killed session
    is_killed = errors > 0 and turn_count > 50

    # Look at last user messages for in-progress clues
    last_context = ""
    if transcript_text:
        last_context = transcript_text[-2000:] if len(transcript_text) > 2000 else transcript_text

    in_progress_markers = [
        "let me", "working on", "next step", "almost done",
        "one more", "still need", "TODO", "WIP", "not yet",
        "in progress", "half done", "continue", "finishing",
    ]

    has_in_progress = any(
        marker.lower() in last_context.lower()
        for marker in in_progress_markers
    ) if last_context else False

    if is_killed or has_in_progress:
        task_name = directive if directive else f"Unfinished work from session {session_id[:12]}"
        tasks.append({
            "task_name": task_name,
            "project": project,
            "company": company,
            "status": "ready",
            "source": "recovery",
            "priority": "medium",
            "notes": (
                f"Recovered from dead session {session_id} ({first_ts}). "
                f"Directive: {directive}"
            ),
            "tier": "auto",
        })

    return tasks


# ---------------------------------------------------------------------------
# check_dedup
# ---------------------------------------------------------------------------
def check_dedup(session_id: str, title: str, source: str = "recovery") -> bool:
    """Check if a recovery item already exists in build_ledger.

    Args:
        session_id: The session UUID.
        title: The item title.
        source: The source channel to check against (default: "recovery").
                Use "recovery_llm" for LLM-pass items so they don't collide
                with regex-pass items that share the same session+title.

    Returns:
        True if a duplicate exists (should skip), False if new.
    """
    encoded_title = urllib.parse.quote(title, safe="")
    url = (
        f"{SUPABASE_URL}/build_ledger"
        f"?session_id=eq.{session_id}"
        f"&title=eq.{encoded_title}"
        f"&source=eq.{source}"
        f"&select=id"
        f"&limit=1"
    )
    req = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return len(data) > 0
    except Exception as exc:
        logger.warning("Dedup check failed for session %s: %s", session_id, exc)
        # On failure, assume no duplicate to avoid data loss
        return False


# ---------------------------------------------------------------------------
# store_items
# ---------------------------------------------------------------------------
def store_items(items: list, table: str = "build_ledger") -> int:
    """POST items to a Supabase table individually.

    Posts one item at a time so individual failures don't block the batch.

    Returns:
        Count of successfully inserted items.
    """
    url = f"{SUPABASE_URL}/{table}"
    headers = {**_HEADERS, "Prefer": "return=minimal"}
    inserted = 0

    for item in items:
        body = json.dumps(item).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 201):
                    inserted += 1
        except Exception as exc:
            logger.error(
                "Failed to insert item into %s: %s — item: %s",
                table, exc, item.get("title", "?")[:80],
            )

    return inserted


# ---------------------------------------------------------------------------
# LLM extraction (deep pass) — uses `claude -p` subprocess
# ---------------------------------------------------------------------------
_LLM_ITEM_TYPES = {
    "vision", "decision", "idea", "gotcha",
    "followup", "open_question", "architecture",
}

_LLM_SYSTEM_PROMPT = (
    "You are a senior engineer reviewing a killed Claude Code session "
    "transcript to recover knowledge that automated regex extraction would "
    "miss. Focus ONLY on the high-value content the regex pass couldn't "
    "capture: architectural decisions with reasoning, product visions, new "
    "feature ideas, open questions, gotchas and lessons learned, and "
    "explicit follow-ups. Do NOT extract routine commits, test additions, "
    "or fixes — those are already captured."
)

_LLM_USER_PROMPT_TEMPLATE = """Session metadata:
  session_id: {sid}
  project: {project}
  company: {company}
  directive: {directive}
  first_ts: {first_ts}

Transcript (USER/ASSISTANT turns only, tool calls stripped):
---BEGIN TRANSCRIPT---
{transcript}
---END TRANSCRIPT---

Extract a JSON array of knowledge items. Each item must have:
  - "title": <=500 chars, specific and actionable (not "discussion about X")
  - "item_type": one of: vision, decision, idea, gotcha, followup, open_question, architecture
  - "summary": 1-3 sentences of what this is and why it matters
  - "excerpt": verbatim quote from the transcript (<=300 chars) as evidence

Return ONLY a JSON array. Empty array [] if nothing worth capturing.
Do NOT extract: commit messages, test additions, routine bug fixes, tool use output.
Focus on: "we should", "we need", "the idea is", "the plan is", "decided to", "turns out", "gotcha:", "next session", "follow up", "vision".
Your entire response must be valid JSON — no preamble, no markdown fences, no trailing commentary."""


def extract_with_llm(
    transcript_text: str,
    session_meta: dict,
    model: str = "claude-sonnet-4-6",
    timeout: int = 180,
) -> list:
    """Extract vision/decision/idea items from a transcript via `claude -p`.

    Spawns a headless Claude Code subprocess with the extraction prompt,
    pipes the prompt via stdin, parses the JSON response, and returns
    ledger-ready dicts.

    Args:
        transcript_text: Trimmed transcript string (output of trim_transcript).
        session_meta: Dict with session_id, project, company, directive,
                      first_ts, slug.
        model: Claude model id to use.
        timeout: Subprocess timeout in seconds.

    Returns:
        List of build_ledger-ready dicts. Empty list on any failure.
    """
    if not transcript_text.strip():
        return []

    sid = session_meta.get("session_id", "")
    project = session_meta.get("project", "general")
    company = session_meta.get("company", "personal")
    directive = session_meta.get("directive", "")
    first_ts = session_meta.get("first_ts", "")
    slug = session_meta.get("slug", "")

    user_prompt = _LLM_USER_PROMPT_TEMPLATE.format(
        sid=sid,
        project=project,
        company=company,
        directive=(directive or "(none)"),
        first_ts=first_ts,
        transcript=transcript_text,
    )

    # Combine system + user into a single prompt (claude -p doesn't take
    # a separate --system flag in all versions).
    full_prompt = f"{_LLM_SYSTEM_PROMPT}\n\n{user_prompt}"

    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--model", model,
                "--output-format", "text",
            ],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("LLM extraction timed out for session %s (%ds)", sid, timeout)
        return []
    except FileNotFoundError:
        logger.error("`claude` CLI not found on PATH")
        return []
    except Exception as exc:
        logger.error("LLM subprocess failed for session %s: %s", sid, exc)
        return []

    if result.returncode != 0:
        logger.warning(
            "claude -p returned %d for session %s: %s",
            result.returncode, sid, (result.stderr or "")[:200],
        )
        return []

    raw = result.stdout or ""
    parsed_items = _parse_llm_json(raw)
    if not parsed_items:
        return []

    # Convert to build_ledger-ready dicts
    notes_base = (
        f"LLM-extracted from dead session {slug or sid} ({first_ts}). "
        f"Directive: {directive or '(none)'}"
    )
    out: list = []
    for it in parsed_items:
        item_type = (it.get("item_type") or "").strip().lower()
        if item_type not in _LLM_ITEM_TYPES:
            # Map unknown types to generic "idea" so nothing is lost
            item_type = "idea"
        title = (it.get("title") or "").strip()[:500]
        if not title:
            continue
        summary = (it.get("summary") or "").strip()
        excerpt = (it.get("excerpt") or "").strip()
        notes = notes_base
        if summary:
            notes = f"{notes}\n\nSummary: {summary}"
        if excerpt:
            notes = f"{notes}\n\nExcerpt: {excerpt}"
        out.append({
            "session_id": sid,
            "project": project,
            "company": company,
            "item_type": item_type,
            "title": title,
            "source": "recovery_llm",
            "test_status": "untested",
            "notes": notes[:4000],
        })
    return out


def _parse_llm_json(raw_text: str) -> list:
    """Tolerant JSON-array parser for LLM output.

    Handles:
      - Markdown code fences (```json ... ```)
      - Leading/trailing prose
      - Trailing commas (best-effort)
      - Validates each item has the required keys before keeping it

    Returns:
        List of dicts with title/item_type/summary/excerpt keys.
        Empty list if nothing parseable.
    """
    if not raw_text:
        return []

    text = raw_text.strip()

    # Strip markdown code fences
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        # Find the first [ and last ] — bracket-matching is overkill for this
        first = text.find("[")
        last = text.rfind("]")
        if first != -1 and last != -1 and last > first:
            text = text[first:last + 1]

    # Try strict parse first
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Try to strip trailing commas before ] or }
        cleaned = re.sub(r",(\s*[\]}])", r"\1", text)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse LLM JSON: %s", str(exc)[:200])
            return []

    if not isinstance(parsed, list):
        logger.warning("LLM output is not a JSON array")
        return []

    required = {"title", "item_type"}
    items = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if not required.issubset(entry.keys()):
            continue
        items.append(entry)
    return items


# ---------------------------------------------------------------------------
# search_transcripts
# ---------------------------------------------------------------------------
def search_transcripts(keyword: str, index: dict, max_workers: int = 8) -> list:
    """Search all transcript files for a keyword.

    Args:
        keyword: Case-insensitive keyword to search for.
        index: Dict mapping session_id -> session index entry. Each entry must
               have a ``transcript_path`` field (or be skipped).
        max_workers: Thread pool size.

    Returns:
        List of match dicts with keys: session_id, slug, project, timestamp,
        context, match_line.
    """
    # Build work list: (session_id, slug, project, transcript_path)
    work = []
    for sid, entry in index.items():
        path = entry.get("transcript_path", "")
        if not path or not Path(path).is_file():
            continue
        project, _ = derive_project_company(entry)
        work.append((sid, entry.get("slug", ""), project, path))

    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    all_matches: list[dict] = []

    def _search_one(item):
        sid, slug, project, path = item
        matches = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    try:
                        obj = json.loads(line_stripped)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type", "")
                    ts = obj.get("timestamp", "")

                    if msg_type == "human":
                        text = _extract_human_text(obj)
                    elif msg_type == "assistant":
                        text = _extract_assistant_text(obj)
                    else:
                        continue

                    if not text:
                        continue

                    m = pattern.search(text)
                    if m:
                        # Extract 500 chars of context around the match
                        start = max(0, m.start() - 250)
                        end = min(len(text), m.end() + 250)
                        context = text[start:end]
                        matches.append({
                            "session_id": sid,
                            "slug": slug,
                            "project": project,
                            "timestamp": ts,
                            "context": context,
                            "match_line": text[:200],
                        })
        except (OSError, IOError) as exc:
            logger.warning("Error reading %s: %s", path, exc)
        return matches

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(_search_one, work):
            all_matches.extend(result)

    return all_matches


# ---------------------------------------------------------------------------
# Self-test (standalone execution)
# ---------------------------------------------------------------------------
def _self_test():
    """Quick self-test: exercise classify, derive, and search on sample data."""
    print("=== extract_session.py self-test ===\n")

    # Test classify_item_type
    cases = [
        ("Fix broken login flow", "fix"),
        ("Add new dashboard widget", "feature"),
        ("Refactor auth module", "chore"),
        ("[DECISION] Use Postgres over SQLite", "decision"),
        ("Update test coverage for API", "test"),
        ("Update README with setup instructions", "docs"),
        ("Implement search functionality", "feature"),
        ("Random commit message", "feature"),
    ]
    print("classify_item_type:")
    all_pass = True
    for text, expected in cases:
        result = classify_item_type(text)
        status = "PASS" if result == expected else "FAIL"
        if status == "FAIL":
            all_pass = False
        print(f"  {status}: '{text}' -> '{result}' (expected '{expected}')")
    print()

    # Test derive_project_company
    derive_cases = [
        ({"project_dir": "/home/user/atlas-portal", "project": "atlas"}, ("atlas", "delphi")),
        ({"project_dir": "/home/user/paperclip", "project": "paperclip"}, ("paperclip", "personal")),
        ({"project_dir": "/home/user/token-watch", "project": "claude-watch"}, ("token-watch", "personal")),
        ({"project_dir": "/home/user/kaa-landscape", "project": "kaa"}, ("kaa", "kaa-landscape")),
        ({"project_dir": "/home/user/cdpc", "project": "frank"}, ("frank-pilot", "frank-pilot")),
        ({"project_dir": "/home/user/misc", "project": "random"}, ("random", "personal")),
    ]
    print("derive_project_company:")
    for entry, expected in derive_cases:
        result = derive_project_company(entry)
        status = "PASS" if result == expected else "FAIL"
        if status == "FAIL":
            all_pass = False
        print(f"  {status}: {entry.get('project_dir')} -> {result} (expected {expected})")
    print()

    # Test extract_metadata_only
    sample_entry = {
        "session_id": "test-uuid-1234",
        "first_ts": "2026-04-06T10:00:00Z",
        "last_ts": "2026-04-06T15:00:00Z",
        "slug": "test-session",
        "directive": "Ship the dashboard",
        "gravity": "Initial release \u2014 Rich-based terminal dashboard (+6 commits)",
        "project": "claude-watch",
        "project_dir": "/Users/a13xperi/projects/token-watch",
        "accomplishments": {
            "git_commits": [
                "Add TUI main screen",
                "Fix token display overflow",
                "Refactor data layer",
            ],
            "errors": 2,
            "turn_count": 100,
        },
    }
    items = extract_metadata_only(sample_entry)
    print(f"extract_metadata_only: generated {len(items)} items")
    for item in items:
        print(f"  [{item['item_type']}] {item['title'][:60]}")
    print()

    # Test trim_transcript with inline data
    print("trim_transcript: ", end="")
    import tempfile
    lines = [
        json.dumps({"type": "human", "message": {"role": "user", "content": "Hello, build the feature"}, "timestamp": "2026-04-06T10:00:00Z"}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Sure, I'll start building."}, {"type": "tool_use", "name": "Edit", "input": {"file": "test.py"}}]}, "timestamp": "2026-04-06T10:00:05Z"}),
        json.dumps({"type": "tool_result", "content": "OK"}),
        json.dumps({"type": "system", "message": "System init"}),
        json.dumps({"type": "human", "message": {"role": "user", "content": "Looks good, ship it"}, "timestamp": "2026-04-06T10:01:00Z"}),
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
        tf.write("\n".join(lines))
        tf_path = tf.name
    result = trim_transcript(tf_path)
    has_user = "[USER @" in result
    has_assistant = "[ASSISTANT @" in result
    has_no_tool = "Edit" not in result
    has_no_system = "System init" not in result
    if has_user and has_assistant and has_no_tool and has_no_system:
        print("PASS")
    else:
        print("FAIL")
        all_pass = False
    Path(tf_path).unlink(missing_ok=True)
    print()

    # Test extract_unfinished_work
    print("extract_unfinished_work: ", end="")
    killed_entry = {
        "session_id": "dead-session-uuid",
        "first_ts": "2026-04-06T10:00:00Z",
        "directive": "Ship the dashboard",
        "project": "claude-watch",
        "project_dir": "/Users/a13xperi/projects/token-watch",
        "accomplishments": {"errors": 5, "turn_count": 100},
    }
    tasks = extract_unfinished_work(killed_entry, "working on the next step...")
    if len(tasks) == 1 and tasks[0]["status"] == "ready":
        print("PASS")
    else:
        print("FAIL")
        all_pass = False
    print()

    # Search transcripts across real session files (if any exist)
    claude_dir = Path.home() / ".claude" / "projects"
    if claude_dir.exists():
        jsonl_files = list(claude_dir.rglob("*.jsonl"))[:5]
        if jsonl_files:
            fake_index = {}
            for i, p in enumerate(jsonl_files):
                sid = f"test-{i}"
                fake_index[sid] = {
                    "slug": p.stem,
                    "project": "test",
                    "project_dir": str(p.parent),
                    "transcript_path": str(p),
                }
            print(f"search_transcripts: searching {len(jsonl_files)} files for 'test'...")
            matches = search_transcripts("test", fake_index, max_workers=4)
            print(f"  Found {len(matches)} matches")
        else:
            print("search_transcripts: no JSONL files found for live test, skipped")
    else:
        print("search_transcripts: no claude projects dir found, skipped")

    print()
    print("Self-test", "PASSED" if all_pass else "FAILED (see above)")
    return 0 if all_pass else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(_self_test())
