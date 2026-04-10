#!/usr/bin/env python3
"""
session_recovery.py — CLI for recovering context from dead Claude Code sessions.

Scans local session transcripts, scores them by value, and extracts
structured knowledge into Supabase (build_ledger / project_tasks).

Modes:
    --scan              Build a ranked manifest of dead sessions
    --search "keyword"  Fast grep across all transcript JSONL files
    --process           Extract and store insights to Supabase
    --status            Show recovery stats

Usage:
    python3 session_recovery.py --scan
    python3 session_recovery.py --search "TUI layout"
    python3 session_recovery.py --process --limit 10 --project atlas
    python3 session_recovery.py --status
"""

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SESSION_INDEX = Path.home() / ".claude/logs/session-index.jsonl"
SESSION_LOGS_DIR = Path.home() / ".claude/session-logs"
MANIFEST_PATH = Path.home() / ".claude/logs/recovery-manifest.json"

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

# Import sibling module for shared extraction logic
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
try:
    from extract_session import (
        classify_item_type,
        derive_project_company,
        extract_metadata_only,
        extract_unfinished_work,
        trim_transcript,
        search_transcripts as _lib_search_transcripts,
        check_dedup,
        store_items,
        extract_with_llm,
    )
except ImportError:
    # Fallback inline implementations if extract_session.py is not available
    print("WARNING: extract_session.py not found, using inline fallbacks", file=sys.stderr)

    def classify_item_type(text: str) -> str:
        low = text.lower()
        if any(kw in low for kw in ("fix", "bug", "patch", "resolve", "hotfix")):
            return "fix"
        if any(kw in low for kw in ("decision", "chose", "decided", "going with", "[decision]")):
            return "decision"
        if any(kw in low for kw in ("refactor", "cleanup", "rename", "lint")):
            return "chore"
        if any(kw in low for kw in ("add", "implement", "ship", "build", "create", "new", "feature")):
            return "feature"
        return "feature"

    def derive_project_company(entry: dict) -> tuple:
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
# Supabase helpers
# ---------------------------------------------------------------------------
def _supa_get(path: str) -> list:
    """GET from Supabase REST API. Returns parsed JSON (list of objects)."""
    url = f"{SUPABASE_URL}/{path}"
    req = urllib.request.Request(url, headers=_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"  WARNING: Supabase GET failed ({path[:60]}): {exc}", file=sys.stderr)
        return []


def _supa_post(table: str, item: dict) -> bool:
    """POST one item to a Supabase table. Returns True on success."""
    url = f"{SUPABASE_URL}/{table}"
    headers = {**_HEADERS, "Prefer": "return=minimal"}
    body = json.dumps(item).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except Exception as exc:
        print(f"  WARNING: Supabase POST to {table} failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Load session index
# ---------------------------------------------------------------------------
def load_index() -> dict:
    """Load session-index.jsonl into a dict keyed by session_id."""
    index = {}
    if not SESSION_INDEX.is_file():
        print(f"ERROR: Session index not found at {SESSION_INDEX}", file=sys.stderr)
        return index
    with open(SESSION_INDEX, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = entry.get("session_id", "")
            if sid:
                # Pre-compute transcript path
                project_dir = entry.get("project_dir", "")
                if project_dir:
                    entry["transcript_path"] = os.path.join(project_dir, f"{sid}.jsonl")
                index[sid] = entry
    return index


# ---------------------------------------------------------------------------
# Score a session
# ---------------------------------------------------------------------------
GRAVITY_KEYWORDS = re.compile(
    r"\b(decision|architecture|plan|design|tui|two|mirror|extension)\b",
    re.IGNORECASE,
)


def score_session(
    entry: dict,
    covered_session_ids: set,
    existing_commit_shas: set,
    existing_recovery_keys: set,
) -> int:
    """Score a session entry for recovery value. Higher = more worth recovering."""
    score = 0
    output_tokens = entry.get("output_tokens", 0)
    accomplishments = entry.get("accomplishments", {})
    turn_count = accomplishments.get("turn_count", 0)
    commits = accomplishments.get("git_commits", [])
    files_edited = accomplishments.get("files_edited", [])
    files_created = accomplishments.get("files_created", [])
    gravity = entry.get("gravity", "")

    # Token volume
    if output_tokens > 50000:
        score += 10
    elif output_tokens > 20000:
        score += 5

    # Git commits (capped)
    score += min(len(commits) * 3, 15)

    # Gravity keywords
    if GRAVITY_KEYWORDS.search(gravity):
        score += 5

    # Turn count
    if turn_count > 100:
        score += 3

    # Files touched
    if len(files_edited) + len(files_created) > 5:
        score += 2

    # Penalties
    sid = entry.get("session_id", "")
    if sid in covered_session_ids:
        score -= 20

    # Check if all commits are already in build_ledger
    if commits and existing_commit_shas:
        # We don't have exact shas from index — skip this heuristic unless
        # all commit messages are already in recovery keys
        all_covered = all(
            f"{sid}:{msg[:500]}" in existing_recovery_keys
            for msg in commits
        )
        if all_covered:
            score -= 10

    return score


# ---------------------------------------------------------------------------
# --scan
# ---------------------------------------------------------------------------
def cmd_scan(args):
    """Build a ranked manifest of dead sessions."""
    print("Loading session index...")
    index = load_index()
    if not index:
        print("No sessions found in index.")
        return 1
    print(f"  Loaded {len(index)} sessions")

    # Find sessions that have SL-*.md close logs
    print("Checking session close logs...")
    covered_session_ids = set()
    if SESSION_LOGS_DIR.is_dir():
        for sl_file in SESSION_LOGS_DIR.iterdir():
            if sl_file.name.startswith("SL-") and sl_file.name.endswith(".md"):
                # Try to find session_id from the file content (first few lines)
                try:
                    text = sl_file.read_text(encoding="utf-8")[:2000]
                    # Look for session_id patterns (UUID)
                    uuid_match = re.search(
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        text,
                    )
                    if uuid_match:
                        covered_session_ids.add(uuid_match.group(0))
                except (OSError, UnicodeDecodeError):
                    pass
    print(f"  Found {len(covered_session_ids)} sessions with close logs")

    # Query Supabase for existing recovery data
    print("Querying Supabase for existing recovery data...")
    existing_commit_shas = set()
    raw_shas = _supa_get("build_ledger?commit_sha=not.is.null&select=commit_sha")
    for row in raw_shas:
        sha = row.get("commit_sha", "")
        if sha:
            existing_commit_shas.add(sha)
    print(f"  Found {len(existing_commit_shas)} commit SHAs in build_ledger")

    existing_recovery_keys = set()
    raw_recovery = _supa_get("build_ledger?source=eq.recovery&select=session_id,title")
    for row in raw_recovery:
        key = f"{row.get('session_id', '')}:{row.get('title', '')}"
        existing_recovery_keys.add(key)
    print(f"  Found {len(raw_recovery)} existing recovery items")

    # Score all sessions
    print("Scoring sessions...")
    scored = []
    for sid, entry in index.items():
        s = score_session(entry, covered_session_ids, existing_commit_shas, existing_recovery_keys)
        scored.append({
            "session_id": sid,
            "score": s,
            "status": "pending",
            "slug": entry.get("slug", ""),
            "project": entry.get("project", ""),
            "output_tokens": entry.get("output_tokens", 0),
            "gravity": entry.get("gravity", ""),
            "first_ts": entry.get("first_ts", ""),
            "last_ts": entry.get("last_ts", ""),
            "turn_count": entry.get("accomplishments", {}).get("turn_count", 0),
            "git_commits": len(entry.get("accomplishments", {}).get("git_commits", [])),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Count dead sessions (score > 0)
    dead_count = sum(1 for s in scored if s["score"] > 0)

    # Save manifest
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(scored),
        "dead": dead_count,
        "sessions": scored,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"\nManifest saved to {MANIFEST_PATH}")

    # Print summary
    print(f"\n{'=' * 70}")
    print(f"SESSION RECOVERY SCAN")
    print(f"{'=' * 70}")
    print(f"Total sessions in index:  {len(scored)}")
    print(f"Dead sessions (score>0):  {dead_count}")
    print(f"Already closed (SL-*.md): {len(covered_session_ids)}")
    print(f"Already in Supabase:      {len(raw_recovery)}")
    print()

    # Top 20
    print(f"{'RANK':>4}  {'SCORE':>5}  {'TOKENS':>8}  {'COMMITS':>7}  {'TURNS':>5}  {'PROJECT':<15}  {'SLUG / GRAVITY'}")
    print(f"{'-' * 4}  {'-' * 5}  {'-' * 8}  {'-' * 7}  {'-' * 5}  {'-' * 15}  {'-' * 40}")
    for i, s in enumerate(scored[:20], 1):
        label = s["slug"] or s["gravity"][:40] or s["session_id"][:12]
        print(
            f"{i:>4}  {s['score']:>5}  {s['output_tokens']:>8}  "
            f"{s['git_commits']:>7}  {s['turn_count']:>5}  "
            f"{s['project']:<15}  {label}"
        )

    return 0


# ---------------------------------------------------------------------------
# --search
# ---------------------------------------------------------------------------
def cmd_search(args):
    """Fast keyword search across all transcript JSONL files."""
    keyword = args.keyword
    if not keyword:
        print("ERROR: --search requires a keyword argument", file=sys.stderr)
        return 1

    print(f"Searching for '{keyword}' across all transcripts...")
    index = load_index()
    if not index:
        print("No sessions found in index.")
        return 1

    # Count how many have actual transcript files
    available = sum(
        1 for e in index.values()
        if e.get("transcript_path") and Path(e["transcript_path"]).is_file()
    )
    print(f"  Scanning {available} transcript files ({len(index)} total sessions)...")

    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    all_matches = []

    def _search_file(item):
        sid, entry = item
        path = entry.get("transcript_path", "")
        if not path or not Path(path).is_file():
            return []
        slug = entry.get("slug", "")
        project, _ = derive_project_company(entry)
        matches = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    # Quick pre-filter before JSON parsing
                    if keyword.lower() not in line_stripped.lower():
                        continue
                    try:
                        obj = json.loads(line_stripped)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type", "")
                    ts = obj.get("timestamp", "")

                    if msg_type in ("human", "user"):
                        text = _extract_human_text(obj)
                    elif msg_type == "assistant":
                        text = _extract_assistant_text(obj)
                    else:
                        continue

                    if not text:
                        continue

                    m = pattern.search(text)
                    if m:
                        start = max(0, m.start() - 250)
                        end = min(len(text), m.end() + 250)
                        context = text[start:end]
                        matches.append({
                            "session_id": sid,
                            "slug": slug,
                            "project": project,
                            "timestamp": ts,
                            "context": context,
                            "speaker": "USER" if msg_type in ("human", "user") else "ASSISTANT",
                        })
        except (OSError, UnicodeDecodeError):
            pass
        return matches

    with ThreadPoolExecutor(max_workers=8) as executor:
        for result in executor.map(_search_file, index.items()):
            all_matches.extend(result)

    if not all_matches:
        print(f"\nNo matches found for '{keyword}'.")
        return 0

    # Group by session
    by_session = {}
    for m in all_matches:
        sid = m["session_id"]
        if sid not in by_session:
            by_session[sid] = {"slug": m["slug"], "project": m["project"], "matches": []}
        by_session[sid]["matches"].append(m)

    print(f"\n{'=' * 70}")
    print(f"SEARCH RESULTS: '{keyword}'")
    print(f"{'=' * 70}")
    print(f"Found {len(all_matches)} matches across {len(by_session)} sessions\n")

    for sid, group in sorted(by_session.items(), key=lambda x: -len(x[1]["matches"])):
        slug = group["slug"] or sid[:12]
        print(f"--- {slug} ({group['project']}) --- {len(group['matches'])} match(es)")
        for i, m in enumerate(group["matches"][:5]):  # Cap at 5 per session
            ts = m.get("timestamp", "?")
            speaker = m["speaker"]
            ctx = m["context"].replace("\n", " ")[:500]
            print(f"  [{speaker} @ {ts}]")
            print(f"  {ctx}")
            print()
        if len(group["matches"]) > 5:
            print(f"  ... and {len(group['matches']) - 5} more matches\n")

    return 0


# ---------------------------------------------------------------------------
# Transcript text extraction (used by search when extract_session not available)
# ---------------------------------------------------------------------------
def _extract_human_text(obj: dict) -> str:
    """Extract text from a human/user-type transcript entry."""
    msg = obj.get("message", {})
    if isinstance(msg, str):
        return msg
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    continue  # skip tool results in search
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
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
# --process
# ---------------------------------------------------------------------------
def cmd_process(args):
    """Extract and store insights to Supabase."""
    # Load or generate manifest
    manifest = None
    if MANIFEST_PATH.is_file():
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (json.JSONDecodeError, OSError):
            manifest = None

    if not manifest:
        print("No manifest found. Running scan first...")
        scan_args = argparse.Namespace()
        cmd_scan(scan_args)
        if not MANIFEST_PATH.is_file():
            print("ERROR: Scan failed to produce manifest.", file=sys.stderr)
            return 1
        with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)

    # Load full index for metadata access
    index = load_index()

    # Filter sessions to process
    sessions = manifest.get("sessions", [])
    pending = [s for s in sessions if s.get("status") == "pending" and s.get("score", 0) > 0]

    # Apply --project filter
    if args.project:
        proj_filter = args.project.lower()
        pending = [s for s in pending if proj_filter in s.get("project", "").lower()]

    # Apply --limit
    limit = args.limit or 20
    to_process = pending[:limit]

    if not to_process:
        print("No pending sessions to process.")
        return 0

    print(f"Processing {len(to_process)} sessions (limit={limit})...")
    print()

    total_ledger = 0
    total_tasks = 0
    deep_candidates = []

    for i, session_rec in enumerate(to_process, 1):
        sid = session_rec["session_id"]
        slug = session_rec.get("slug", "") or sid[:12]
        score = session_rec.get("score", 0)
        entry = index.get(sid)

        if not entry:
            print(f"  [{i}/{len(to_process)}] {slug}: not in index, skipping")
            session_rec["status"] = "skipped"
            continue

        print(f"  [{i}/{len(to_process)}] {slug} (score={score})")

        # -- Metadata extraction --
        items = []
        project, company = derive_project_company(entry)
        accomplishments = entry.get("accomplishments", {})
        directive = entry.get("directive", "")
        gravity = entry.get("gravity", "")
        first_ts = entry.get("first_ts", "")
        base_notes = f"Directive: {directive}. Recovered from session {slug} ({first_ts})"

        # One item per git commit
        for commit_msg in accomplishments.get("git_commits", []):
            items.append({
                "session_id": sid,
                "project": project,
                "company": company,
                "item_type": classify_item_type(commit_msg),
                "title": commit_msg[:500],
                "source": "recovery",
                "test_status": "untested",
                "notes": base_notes,
            })

        # Gravity summary item (if non-trivial)
        if gravity and not re.match(r"^session\s*\(\d+\s+turns?\)$", gravity, re.IGNORECASE):
            items.append({
                "session_id": sid,
                "project": project,
                "company": company,
                "item_type": "feature",
                "title": gravity[:500],
                "source": "recovery",
                "test_status": "untested",
                "notes": base_notes,
            })

        # Dedup and store to build_ledger
        new_items = []
        for item in items:
            # Check for existing duplicate
            encoded_title = urllib.parse.quote(item["title"], safe="")
            existing = _supa_get(
                f"build_ledger?session_id=eq.{sid}"
                f"&title=eq.{encoded_title}"
                f"&source=eq.recovery&select=id&limit=1"
            )
            if not existing:
                new_items.append(item)

        if new_items:
            inserted = 0
            for item in new_items:
                if _supa_post("build_ledger", item):
                    inserted += 1
            total_ledger += inserted
            print(f"    -> {inserted} items stored to build_ledger ({len(items) - len(new_items)} deduped)")
        else:
            print(f"    -> all {len(items)} items already in build_ledger")

        # -- Unfinished work detection (regex pass) --
        # DISABLED 2026-04-09: the session "directive" field is often just the
        # first user message, so this pass produced 206 garbage rows
        # ("kill 74048", "Yeah do that", "[Image #6]...", agent UUIDs, etc.).
        # The recovery_llm pass handles unfinished work properly via real LLM
        # extraction — see _process_deep() and store as source=recovery_llm.
        pass

        # -- Deep extraction candidate check --
        output_tokens = entry.get("output_tokens", 0)
        if score > 15 and output_tokens > 20000:
            transcript_path = entry.get("transcript_path", "")
            if transcript_path and Path(transcript_path).is_file():
                deep_candidates.append({
                    "session_id": sid,
                    "slug": slug,
                    "score": score,
                    "output_tokens": output_tokens,
                    "transcript_path": transcript_path,
                })

        session_rec["status"] = "processed"

    # Save updated manifest
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    # Summary
    print(f"\n{'=' * 70}")
    print(f"PROCESSING COMPLETE")
    print(f"{'=' * 70}")
    print(f"Sessions processed:    {len(to_process)}")
    print(f"Build ledger items:    {total_ledger}")
    print(f"Project tasks created: {total_tasks}")
    print(f"Manifest updated:      {MANIFEST_PATH}")

    if deep_candidates:
        print(f"\nDEEP EXTRACTION CANDIDATES ({len(deep_candidates)}):")
        print(f"These sessions have rich transcripts worth LLM analysis.")
        print(f"Run /recover-sessions in Claude Code to process them.\n")
        for dc in deep_candidates[:10]:
            print(
                f"  {dc['slug']:<35} score={dc['score']:>3}  "
                f"tokens={dc['output_tokens']:>8}  {dc['transcript_path']}"
            )
        if len(deep_candidates) > 10:
            print(f"  ... and {len(deep_candidates) - 10} more")

    return 0


# ---------------------------------------------------------------------------
# --deep (LLM extraction pass on rich transcripts)
# ---------------------------------------------------------------------------
def cmd_deep_extract(args):
    """Run an LLM extraction pass over the 86 deep-candidate sessions.

    Criteria: score > 15 AND output_tokens > 20000. The regex pass already
    processed these but only caught commit-style content. The LLM pass
    captures vision/decision/idea/architecture content that would otherwise
    be lost.

    Writes to build_ledger with source='recovery_llm' so it co-exists with
    (and doesn't collide against) the regex pass 'recovery' items. Marks
    each session with deep_extracted=True in the manifest for resumability.
    """
    if not MANIFEST_PATH.is_file():
        print("No manifest found. Run --scan first.", file=sys.stderr)
        return 1

    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Failed to load manifest: {exc}", file=sys.stderr)
        return 1

    index = load_index()

    sessions = manifest.get("sessions", [])
    candidates = []
    for s in sessions:
        if s.get("deep_extracted"):
            continue
        sid = s.get("session_id")
        entry = index.get(sid)
        if not entry:
            continue
        score = s.get("score", 0)
        output_tokens = entry.get("output_tokens", 0)
        if score <= 15 or output_tokens <= 20000:
            continue
        transcript_path = entry.get("transcript_path", "")
        if not transcript_path or not Path(transcript_path).is_file():
            continue
        candidates.append((s, entry))

    # Sort by richness: highest score then highest output_tokens
    candidates.sort(
        key=lambda x: (x[0].get("score", 0), x[1].get("output_tokens", 0)),
        reverse=True,
    )

    if args.project:
        proj_filter = args.project.lower()
        candidates = [
            c for c in candidates
            if proj_filter in (c[0].get("project", "") or "").lower()
        ]

    limit = args.limit or 20
    to_process = candidates[:limit]

    if not to_process:
        print("No deep-extraction candidates remaining.")
        return 0

    print(f"Deep LLM extraction: {len(to_process)} sessions (limit={limit})")
    print(f"Total pending deep candidates: {len(candidates)}")
    print(f"Transport: claude -p --model claude-sonnet-4-6")
    print()

    total_items = 0
    total_tasks = 0
    total_skipped = 0
    failed_sessions = []

    for i, (session_rec, entry) in enumerate(to_process, 1):
        sid = session_rec["session_id"]
        slug = session_rec.get("slug", "") or sid[:12]
        score = session_rec.get("score", 0)
        output_tokens = entry.get("output_tokens", 0)
        transcript_path = entry.get("transcript_path", "")

        print(f"  [{i}/{len(to_process)}] {slug} "
              f"(score={score}, tokens={output_tokens})")

        # Trim transcript — 200k chars cap for LLM (Sonnet has 1M context)
        transcript_text = trim_transcript(transcript_path, max_chars=200_000)
        if not transcript_text.strip():
            print(f"    -> empty transcript, skipping")
            total_skipped += 1
            session_rec["deep_extracted"] = True
            _save_manifest(manifest)
            continue

        project, company = derive_project_company(entry)
        session_meta = {
            "session_id": sid,
            "slug": slug,
            "project": project,
            "company": company,
            "directive": entry.get("directive", ""),
            "first_ts": entry.get("first_ts", ""),
        }

        # Call the LLM
        items = extract_with_llm(transcript_text, session_meta)
        if not items:
            print(f"    -> 0 items extracted (empty or parse failure)")
            session_rec["deep_extracted"] = True
            _save_manifest(manifest)
            continue

        # Dedup against prior LLM-pass runs on this session
        new_items = []
        for item in items:
            if not check_dedup(sid, item["title"], source="recovery_llm"):
                new_items.append(item)

        inserted = store_items(new_items, table="build_ledger") if new_items else 0
        total_items += inserted

        # Breakdown by item_type
        type_breakdown = {}
        for it in new_items:
            t = it.get("item_type", "unknown")
            type_breakdown[t] = type_breakdown.get(t, 0) + 1
        breakdown_str = ", ".join(
            f"{c} {t}" for t, c in sorted(type_breakdown.items(), key=lambda x: -x[1])
        ) if type_breakdown else "none"

        deduped_count = len(items) - len(new_items)
        dedup_note = f" ({deduped_count} deduped)" if deduped_count else ""
        print(f"    -> {inserted} items stored: {breakdown_str}{dedup_note}")

        # Spawn project_tasks for followup / open_question items
        task_items = [
            it for it in new_items
            if it.get("item_type") in ("followup", "open_question")
        ]
        tasks_created = 0
        for item in task_items:
            task_name = item["title"][:500]
            task = {
                "task_name": task_name,
                "project": item["project"],
                "company": item["company"],
                "status": "ready",
                "source": "recovery_llm",
                "priority": "medium",
                "notes": item.get("notes", ""),
                "tier": "auto",
            }
            encoded_task = urllib.parse.quote(task_name, safe="")
            existing = _supa_get(
                f"project_tasks?task_name=eq.{encoded_task}"
                f"&source=eq.recovery_llm&select=id&limit=1"
            )
            if not existing and _supa_post("project_tasks", task):
                tasks_created += 1
        if tasks_created:
            print(f"    -> {tasks_created} project_tasks created "
                  f"(followup/open_question)")
        total_tasks += tasks_created

        session_rec["deep_extracted"] = True
        _save_manifest(manifest)  # crash-safe resumption

    print(f"\n{'=' * 70}")
    print(f"DEEP EXTRACTION COMPLETE")
    print(f"{'=' * 70}")
    print(f"Sessions processed:    {len(to_process)}")
    print(f"Sessions skipped:      {total_skipped}")
    print(f"LLM items stored:      {total_items}")
    print(f"Project tasks created: {total_tasks}")
    print(f"Pending deep candidates remaining: {len(candidates) - len(to_process)}")
    if failed_sessions:
        print(f"\nFailed sessions ({len(failed_sessions)}):")
        for fs in failed_sessions[:10]:
            print(f"  {fs}")
    return 0


def _save_manifest(manifest: dict) -> None:
    """Save the manifest to disk (helper for crash-safe deep extraction)."""
    try:
        with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
    except OSError as exc:
        print(f"  WARNING: failed to save manifest: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------
def cmd_status(args):
    """Show recovery stats."""
    # Load manifest
    manifest = None
    if MANIFEST_PATH.is_file():
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (json.JSONDecodeError, OSError):
            manifest = None

    if not manifest:
        print("No manifest found. Run --scan first.")
        return 1

    sessions = manifest.get("sessions", [])
    total = manifest.get("total", len(sessions))
    dead = manifest.get("dead", 0)
    generated_at = manifest.get("generated_at", "?")

    # Count statuses
    pending = sum(1 for s in sessions if s.get("status") == "pending")
    processed = sum(1 for s in sessions if s.get("status") == "processed")
    skipped = sum(1 for s in sessions if s.get("status") == "skipped")

    # Query Supabase for recovery item counts
    print("Querying Supabase...")
    recovery_items = _supa_get("build_ledger?source=eq.recovery&select=id")
    recovery_tasks = _supa_get("project_tasks?source=eq.recovery&select=id")

    # Breakdown by item_type
    recovery_by_type = _supa_get(
        "build_ledger?source=eq.recovery&select=item_type"
    )
    type_counts = {}
    for row in recovery_by_type:
        t = row.get("item_type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    # Breakdown by project
    recovery_by_project = _supa_get(
        "build_ledger?source=eq.recovery&select=project"
    )
    project_counts = {}
    for row in recovery_by_project:
        p = row.get("project", "unknown")
        project_counts[p] = project_counts.get(p, 0) + 1

    print(f"\n{'=' * 70}")
    print(f"SESSION RECOVERY STATUS")
    print(f"{'=' * 70}")
    print(f"Manifest generated: {generated_at}")
    print()
    print(f"  Total in manifest:  {total}")
    print(f"  Dead (score > 0):   {dead}")
    print(f"  Pending:            {pending}")
    print(f"  Processed:          {processed}")
    print(f"  Skipped:            {skipped}")
    print()
    print(f"SUPABASE:")
    print(f"  Build ledger items: {len(recovery_items)}")
    print(f"  Project tasks:      {len(recovery_tasks)}")

    if type_counts:
        print(f"\n  By type:")
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"    {t:<15} {c}")

    if project_counts:
        print(f"\n  By project:")
        for p, c in sorted(project_counts.items(), key=lambda x: -x[1]):
            print(f"    {p:<20} {c}")

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Recover context from dead Claude Code sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 session_recovery.py --scan
  python3 session_recovery.py --search "TUI layout"
  python3 session_recovery.py --process --limit 10
  python3 session_recovery.py --process --project atlas --limit 5
  python3 session_recovery.py --status
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--scan",
        action="store_true",
        help="Build a ranked manifest of dead sessions",
    )
    group.add_argument(
        "--search",
        metavar="KEYWORD",
        dest="keyword",
        help="Fast grep across all transcript JSONL files",
    )
    group.add_argument(
        "--process",
        action="store_true",
        help="Extract and store insights to Supabase",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Show recovery stats",
    )
    group.add_argument(
        "--deep",
        action="store_true",
        help="LLM extraction pass over rich (deep-candidate) sessions",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max sessions to process (default: 20, used with --process)",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Filter to a specific project (used with --process)",
    )

    args = parser.parse_args()

    if args.scan:
        return cmd_scan(args)
    elif args.keyword:
        return cmd_search(args)
    elif args.process:
        return cmd_process(args)
    elif args.status:
        return cmd_status(args)
    elif args.deep:
        return cmd_deep_extract(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
