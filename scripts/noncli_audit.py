#!/usr/bin/env python3
"""
noncli_audit.py — Audit non-CLI Claude Code sessions by token usage.

Shows what Paperclip agents, SAGE agents, and other non-CLI sessions
spent their output tokens on, with the ability to read actual transcripts.

Usage:
    python3 noncli_audit.py                     # top 20 non-CLI sessions by output tokens
    python3 noncli_audit.py --top 50            # top 50
    python3 noncli_audit.py --source "Delphi"   # filter by source prefix
    python3 noncli_audit.py --summary           # grouped summary by source
    python3 noncli_audit.py --read <session_id> # print transcript of a session
    python3 noncli_audit.py --search "keyword"  # search across all non-CLI transcripts
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

INDEX_PATH = Path.home() / ".claude/logs/session-index.jsonl"


# ---------------------------------------------------------------------------
# Load index
# ---------------------------------------------------------------------------

def load_index(source_filter: str = None) -> list[dict]:
    sessions = []
    with open(INDEX_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            src = obj.get("source", "")
            if src == "cli":
                continue
            if source_filter and source_filter.lower() not in src.lower():
                continue
            sessions.append(obj)
    return sessions


# ---------------------------------------------------------------------------
# Resolve transcript path
# ---------------------------------------------------------------------------

def resolve_transcript(session: dict) -> Path | None:
    project_dir = session.get("project_dir", "")
    session_id = session.get("session_id", "")
    if not project_dir or not session_id:
        return None
    p = Path(project_dir) / f"{session_id}.jsonl"
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Read transcript
# ---------------------------------------------------------------------------

def read_transcript(path: Path, max_chars: int = 80_000) -> str:
    parts = []
    try:
        with open(path) as f:
            for line in f:
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
                    msg = obj.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in content
                        )
                    else:
                        text = str(content)
                    if text.strip():
                        parts.append(f"\033[36m[USER @ {ts}]\033[0m\n{text.strip()}\n")

                elif msg_type == "assistant":
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    texts = []
                    tool_names = []
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    t = block.get("text", "").strip()
                                    if t:
                                        texts.append(t)
                                elif block.get("type") == "tool_use":
                                    tool_names.append(block.get("name", "?"))
                    elif isinstance(content, str):
                        texts = [content.strip()]

                    body = "\n".join(texts)
                    tool_note = f"  \033[33m[tools: {', '.join(tool_names)}]\033[0m" if tool_names else ""
                    if body or tool_note:
                        parts.append(f"\033[32m[ASSISTANT @ {ts}]\033[0m{tool_note}\n{body}\n")

    except (OSError, IOError) as e:
        return f"[Error reading transcript: {e}]"

    full = "\n".join(parts)
    if len(full) > max_chars:
        third = max_chars // 3
        full = (
            full[:third]
            + f"\n\n\033[31m... [{len(full) - max_chars*2//3:,} chars trimmed from middle] ...\033[0m\n\n"
            + full[-third:]
        )
    return full


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(sessions: list[dict], top: int):
    sessions_sorted = sorted(sessions, key=lambda s: s.get("output_tokens", 0), reverse=True)
    top_sessions = sessions_sorted[:top]

    total_out = sum(s.get("output_tokens", 0) for s in sessions)
    total_in = sum(
        (s.get("accomplishments", {}) or {}).get("input_tokens", 0)
        for s in sessions
    )

    print(f"\n\033[1mNon-CLI Sessions — Top {top} by Output Tokens\033[0m")
    print(f"Total sessions: {len(sessions)}  |  Total output tokens: {total_out:,}")
    print()
    print(f"{'#':>3}  {'OUTPUT':>9}  {'SOURCE':<26}  {'DATE':<12}  {'DIRECTIVE / SLUG'}")
    print("-" * 100)

    for i, s in enumerate(top_sessions, 1):
        out_tok = s.get("output_tokens", 0)
        src = s.get("source", "?")[:26]
        ts = (s.get("first_ts") or "")[:10]
        directive = s.get("directive") or s.get("slug") or s.get("session_id", "")[:20]
        directive = directive[:50]
        sid = s.get("session_id", "")[:8]
        has_transcript = "✓" if resolve_transcript(s) else " "
        print(f"{i:>3}  {out_tok:>9,}  {src:<26}  {ts:<12}  {has_transcript} {directive}")

    print()
    print(f"✓ = transcript readable | run --read <session_id> to view")


def cmd_summary(sessions: list[dict]):
    by_source = defaultdict(lambda: {"count": 0, "output_tokens": 0, "sessions": []})
    for s in sessions:
        src = s.get("source", "unknown")
        by_source[src]["count"] += 1
        by_source[src]["output_tokens"] += s.get("output_tokens", 0)
        by_source[src]["sessions"].append(s)

    ranked = sorted(by_source.items(), key=lambda x: x[1]["output_tokens"], reverse=True)
    total = sum(v["output_tokens"] for _, v in ranked)

    print(f"\n\033[1mNon-CLI Token Summary by Source\033[0m")
    print(f"Total: {total:,} output tokens across {len(sessions)} sessions\n")
    print(f"{'SOURCE':<30}  {'SESSIONS':>8}  {'OUTPUT TOK':>12}  {'% OF TOTAL':>10}  {'AVG/SESSION':>12}")
    print("-" * 85)

    for src, data in ranked:
        pct = data["output_tokens"] / total * 100 if total else 0
        avg = data["output_tokens"] // data["count"] if data["count"] else 0
        # Top directive for this source
        top_sess = sorted(data["sessions"], key=lambda s: s.get("output_tokens", 0), reverse=True)
        example = (top_sess[0].get("directive") or top_sess[0].get("slug") or "")[:30] if top_sess else ""
        print(f"{src:<30}  {data['count']:>8,}  {data['output_tokens']:>12,}  {pct:>9.1f}%  {avg:>12,}")

    print()
    print("Tip: filter with --source <name> to drill into a specific agent")


def cmd_read(sessions: list[dict], session_id: str):
    # Accept prefix match
    matches = [s for s in sessions if s.get("session_id", "").startswith(session_id)]

    if not matches:
        # Try across ALL sessions including CLI
        all_sessions = load_all_sessions()
        matches = [s for s in all_sessions if s.get("session_id", "").startswith(session_id)]

    if not matches:
        print(f"No session found with id prefix: {session_id}")
        sys.exit(1)

    s = matches[0]
    transcript_path = resolve_transcript(s)

    print(f"\n\033[1mSession: {s.get('session_id')}\033[0m")
    print(f"Source:    {s.get('source', '?')}")
    print(f"Date:      {(s.get('first_ts') or '')[:19]}")
    print(f"Directive: {s.get('directive', '(none)')}")
    print(f"Output:    {s.get('output_tokens', 0):,} tokens")
    print(f"Turns:     {(s.get('accomplishments') or {}).get('turn_count', '?')}")
    print(f"File:      {transcript_path or 'NOT FOUND'}")
    print("-" * 80)

    if not transcript_path:
        print("\n[Transcript file not found on disk]")
        return

    text = read_transcript(transcript_path)
    if text:
        print(text)
    else:
        print("[Empty transcript]")


def cmd_search(sessions: list[dict], keyword: str):
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    results = []

    for s in sessions:
        path = resolve_transcript(s)
        if not path:
            continue
        try:
            with open(path) as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    msg_type = obj.get("type", "")
                    if msg_type not in ("human", "assistant"):
                        continue
                    msg = obj.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in content
                        )
                    else:
                        text = str(content)
                    m = pattern.search(text)
                    if m:
                        start = max(0, m.start() - 150)
                        end = min(len(text), m.end() + 150)
                        results.append({
                            "session_id": s.get("session_id", "")[:8],
                            "source": s.get("source", "?"),
                            "ts": (obj.get("timestamp") or "")[:19],
                            "type": msg_type,
                            "context": text[start:end].replace("\n", " "),
                        })
                        break  # one match per session
        except (OSError, IOError):
            continue

    if not results:
        print(f"No matches for '{keyword}' in non-CLI transcripts")
        return

    print(f"\n\033[1mSearch: '{keyword}' — {len(results)} sessions matched\033[0m\n")
    for r in results:
        print(f"\033[33m[{r['source']}]\033[0m {r['session_id']}  {r['ts']}  ({r['type']})")
        print(f"  ...{r['context']}...")
        print()


def load_all_sessions() -> list[dict]:
    sessions = []
    with open(INDEX_PATH) as f:
        for line in f:
            try:
                sessions.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return sessions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Audit non-CLI Claude Code sessions")
    parser.add_argument("--top", type=int, default=20, help="Number of sessions to show (default: 20)")
    parser.add_argument("--source", type=str, default=None, help="Filter by source name (partial match)")
    parser.add_argument("--summary", action="store_true", help="Show grouped summary by source")
    parser.add_argument("--read", type=str, metavar="SESSION_ID", help="Print transcript of a session")
    parser.add_argument("--search", type=str, metavar="KEYWORD", help="Search across non-CLI transcripts")
    parser.add_argument("--all", action="store_true", help="Include CLI sessions too")
    args = parser.parse_args()

    if not INDEX_PATH.exists():
        print(f"Session index not found: {INDEX_PATH}")
        print("Run the token-watch indexer first.")
        sys.exit(1)

    if args.read:
        # For --read, load non-cli sessions but fallback handled inside
        sessions = load_index(args.source)
        cmd_read(sessions, args.read)
        return

    sessions = load_index(args.source) if not args.all else load_all_sessions()

    if args.search:
        cmd_search(sessions, args.search)
    elif args.summary:
        cmd_summary(sessions)
    else:
        cmd_list(sessions, args.top)


if __name__ == "__main__":
    main()
