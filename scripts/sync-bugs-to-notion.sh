#!/usr/bin/env bash
# sync-bugs-to-notion.sh — Mirror Supabase bugs + test_cases to Notion databases
#
# Requirements:
#   NOTION_TOKEN   — Notion internal integration token (create at https://www.notion.so/my-integrations)
#   NOTION_BUGS_DB — Notion database ID for bugs
#   NOTION_TESTS_DB — Notion database ID for test_cases
#   SUPA_URL       — Supabase REST URL (default: atlas project)
#   SUPA_KEY       — Supabase anon key (default: atlas project)
#
# Usage:
#   ./sync-bugs-to-notion.sh              # sync both tables
#   ./sync-bugs-to-notion.sh bugs         # sync bugs only
#   ./sync-bugs-to-notion.sh test_cases   # sync test_cases only
#   ./sync-bugs-to-notion.sh --dry-run    # show what would sync without writing
#
# Notion database setup:
#   Bugs DB needs these properties:
#     supabase_id (rich_text), bug_number (number), title (title), severity (select),
#     status (select), page_route (rich_text), project (rich_text), source (rich_text),
#     description (rich_text), steps_to_reproduce (rich_text), branch (rich_text),
#     repo (rich_text), file_path (rich_text), found_by (rich_text), fixed_by (rich_text),
#     pr_url (url), tags (multi_select), created_at (date), updated_at (date),
#     found_at (date), fixed_at (date), verified_at (date)
#
#   Test Cases DB needs these properties:
#     supabase_id (rich_text), test_number (number), title (title), category (select),
#     page_route (rich_text), priority (select), unit_status (select),
#     e2e_status (select), user_status (select), repo (rich_text),
#     description (rich_text), user_tester (rich_text), created_by (rich_text),
#     created_at (date), updated_at (date)

set -euo pipefail

# ─── Defaults ───────────────────────────────────────────────────────────────
SUPA_URL="${SUPA_URL:-https://zoirudjyqfqvpxsrxepr.supabase.co}"
SUPA_KEY="${SUPA_KEY:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpvaXJ1ZGp5cWZxdnB4c3J4ZXByIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjgwMzE4MjgsImV4cCI6MjA4MzYwNzgyOH0.6W6OzRfJ-nmKN_23z1OBCS4Cr-ODRq9DJmF_yMwOCfo}"

DRY_RUN=false
SYNC_TARGET="${1:-all}"

if [[ "$SYNC_TARGET" == "--dry-run" ]]; then
  DRY_RUN=true
  SYNC_TARGET="${2:-all}"
fi

# ─── Validate ───────────────────────────────────────────────────────────────
if [[ -z "${NOTION_TOKEN:-}" ]]; then
  echo "ERROR: NOTION_TOKEN not set."
  echo ""
  echo "To set up:"
  echo "  1. Go to https://www.notion.so/my-integrations"
  echo "  2. Create a new integration with read+write access"
  echo "  3. Share your target databases with the integration"
  echo "  4. Export NOTION_TOKEN=ntn_XXXXXXX"
  echo "  5. Export NOTION_BUGS_DB=<database-id-from-url>"
  echo "  6. Export NOTION_TESTS_DB=<database-id-from-url>"
  exit 1
fi

if [[ -z "${NOTION_BUGS_DB:-}" && ("$SYNC_TARGET" == "all" || "$SYNC_TARGET" == "bugs") ]]; then
  echo "ERROR: NOTION_BUGS_DB not set. Set it to the Notion database ID for bugs."
  exit 1
fi

if [[ -z "${NOTION_TESTS_DB:-}" && ("$SYNC_TARGET" == "all" || "$SYNC_TARGET" == "test_cases") ]]; then
  echo "ERROR: NOTION_TESTS_DB not set. Set it to the Notion database ID for test cases."
  exit 1
fi

# ─── Helpers ────────────────────────────────────────────────────────────────

supa_get() {
  local table="$1"
  curl -s "${SUPA_URL}/rest/v1/${table}?select=*" \
    -H "apikey: ${SUPA_KEY}" \
    -H "Authorization: Bearer ${SUPA_KEY}"
}

notion_query_db() {
  local db_id="$1"
  local filter="$2"
  curl -s -X POST "https://api.notion.com/v1/databases/${db_id}/query" \
    -H "Authorization: Bearer ${NOTION_TOKEN}" \
    -H "Notion-Version: 2022-06-28" \
    -H "Content-Type: application/json" \
    -d "$filter"
}

notion_create_page() {
  local payload="$1"
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY RUN] Would create page"
    return 0
  fi
  curl -s -X POST "https://api.notion.com/v1/pages" \
    -H "Authorization: Bearer ${NOTION_TOKEN}" \
    -H "Notion-Version: 2022-06-28" \
    -H "Content-Type: application/json" \
    -d "$payload"
}

notion_update_page() {
  local page_id="$1"
  local payload="$2"
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY RUN] Would update page ${page_id}"
    return 0
  fi
  curl -s -X PATCH "https://api.notion.com/v1/pages/${page_id}" \
    -H "Authorization: Bearer ${NOTION_TOKEN}" \
    -H "Notion-Version: 2022-06-28" \
    -H "Content-Type: application/json" \
    -d "$payload"
}

# Build a JSON text property value, handling nulls
jq_text() {
  local val="$1"
  if [[ "$val" == "null" || -z "$val" ]]; then
    echo '{"rich_text":[]}'
  else
    python3 -c "import json; print(json.dumps({'rich_text':[{'text':{'content':json.loads(json.dumps('$val'))}}]}))" 2>/dev/null \
      || echo '{"rich_text":[]}'
  fi
}

# Find existing Notion page by supabase_id in a database
find_notion_page() {
  local db_id="$1"
  local supa_id="$2"
  local filter
  filter=$(python3 -c "
import json
print(json.dumps({
  'filter': {
    'property': 'supabase_id',
    'rich_text': {'equals': '$supa_id'}
  }
}))
")
  local result
  result=$(notion_query_db "$db_id" "$filter")
  echo "$result" | python3 -c "
import json, sys
data = json.load(sys.stdin)
results = data.get('results', [])
if results:
    print(results[0]['id'])
else:
    print('')
" 2>/dev/null || echo ""
}

# ─── Bug sync ──────────────────────────────────────────────────────────────

sync_bugs() {
  echo "=== Syncing bugs to Notion ==="
  local bugs
  bugs=$(supa_get "bugs")
  local count
  count=$(echo "$bugs" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
  echo "Found ${count} bugs in Supabase"

  echo "$bugs" | python3 -c "
import json, sys, subprocess, os

DRY_RUN = os.environ.get('DRY_RUN_PY', 'false') == 'true'
NOTION_TOKEN = os.environ['NOTION_TOKEN']
NOTION_BUGS_DB = os.environ['NOTION_BUGS_DB']

bugs = json.load(sys.stdin)
created = 0
updated = 0
skipped = 0

for bug in bugs:
    supa_id = bug['id']

    # Check if page exists
    filter_payload = json.dumps({
        'filter': {
            'property': 'supabase_id',
            'rich_text': {'equals': supa_id}
        }
    })

    import urllib.request
    req = urllib.request.Request(
        f'https://api.notion.com/v1/databases/{NOTION_BUGS_DB}/query',
        data=filter_payload.encode(),
        headers={
            'Authorization': f'Bearer {NOTION_TOKEN}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json'
        },
        method='POST'
    )

    try:
        resp = urllib.request.urlopen(req)
        query_result = json.loads(resp.read())
        existing_pages = query_result.get('results', [])
    except Exception as e:
        print(f'  ERROR querying Notion for bug {supa_id}: {e}')
        skipped += 1
        continue

    def text_prop(val):
        if val is None:
            return {'rich_text': []}
        return {'rich_text': [{'text': {'content': str(val)[:2000]}}]}

    def title_prop(val):
        return {'title': [{'text': {'content': str(val or 'Untitled')[:2000]}}]}

    def number_prop(val):
        return {'number': val}

    def select_prop(val):
        if val is None:
            return {'select': None}
        return {'select': {'name': str(val)}}

    def date_prop(val):
        if val is None:
            return {'date': None}
        return {'date': {'start': val}}

    def url_prop(val):
        if val is None or val == '':
            return {'url': None}
        return {'url': str(val)}

    def multi_select_prop(vals):
        if not vals:
            return {'multi_select': []}
        return {'multi_select': [{'name': str(v)} for v in vals]}

    properties = {
        'title': title_prop(bug.get('title')),
        'supabase_id': text_prop(supa_id),
        'bug_number': number_prop(bug.get('bug_number')),
        'severity': select_prop(bug.get('severity')),
        'status': select_prop(bug.get('status')),
        'page_route': text_prop(bug.get('page_route')),
        'project': text_prop(bug.get('project')),
        'source': text_prop(bug.get('source')),
        'description': text_prop(bug.get('description')),
        'steps_to_reproduce': text_prop(bug.get('steps_to_reproduce')),
        'branch': text_prop(bug.get('branch')),
        'repo': text_prop(bug.get('repo')),
        'file_path': text_prop(bug.get('file_path')),
        'found_by': text_prop(bug.get('found_by')),
        'fixed_by': text_prop(bug.get('fixed_by')),
        'pr_url': url_prop(bug.get('pr_url')),
        'tags': multi_select_prop(bug.get('tags', [])),
        'created_at': date_prop(bug.get('created_at')),
        'updated_at': date_prop(bug.get('updated_at')),
        'found_at': date_prop(bug.get('found_at')),
        'fixed_at': date_prop(bug.get('fixed_at')),
        'verified_at': date_prop(bug.get('verified_at')),
    }

    if existing_pages:
        page_id = existing_pages[0]['id']
        # Remove title from update (can't update title via properties patch easily)
        update_props = {k: v for k, v in properties.items()}
        payload = json.dumps({'properties': update_props})

        if DRY_RUN:
            print(f'  [DRY RUN] Would update bug #{bug.get(\"bug_number\")}: {bug.get(\"title\")[:50]}')
            updated += 1
            continue

        req2 = urllib.request.Request(
            f'https://api.notion.com/v1/pages/{page_id}',
            data=payload.encode(),
            headers={
                'Authorization': f'Bearer {NOTION_TOKEN}',
                'Notion-Version': '2022-06-28',
                'Content-Type': 'application/json'
            },
            method='PATCH'
        )
        try:
            urllib.request.urlopen(req2)
            updated += 1
            print(f'  Updated bug #{bug.get(\"bug_number\")}: {bug.get(\"title\")[:50]}')
        except Exception as e:
            print(f'  ERROR updating bug #{bug.get(\"bug_number\")}: {e}')
            skipped += 1
    else:
        payload = json.dumps({
            'parent': {'database_id': NOTION_BUGS_DB},
            'properties': properties
        })

        if DRY_RUN:
            print(f'  [DRY RUN] Would create bug #{bug.get(\"bug_number\")}: {bug.get(\"title\")[:50]}')
            created += 1
            continue

        req2 = urllib.request.Request(
            'https://api.notion.com/v1/pages',
            data=payload.encode(),
            headers={
                'Authorization': f'Bearer {NOTION_TOKEN}',
                'Notion-Version': '2022-06-28',
                'Content-Type': 'application/json'
            },
            method='POST'
        )
        try:
            urllib.request.urlopen(req2)
            created += 1
            print(f'  Created bug #{bug.get(\"bug_number\")}: {bug.get(\"title\")[:50]}')
        except Exception as e:
            print(f'  ERROR creating bug #{bug.get(\"bug_number\")}: {e}')
            skipped += 1

print(f'\\nBugs sync complete: {created} created, {updated} updated, {skipped} skipped')
" DRY_RUN_PY="$DRY_RUN"
}

# ─── Test cases sync ───────────────────────────────────────────────────────

sync_test_cases() {
  echo "=== Syncing test_cases to Notion ==="
  local tests
  tests=$(supa_get "test_cases")
  local count
  count=$(echo "$tests" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
  echo "Found ${count} test cases in Supabase"

  echo "$tests" | python3 -c "
import json, sys, urllib.request, os

DRY_RUN = os.environ.get('DRY_RUN_PY', 'false') == 'true'
NOTION_TOKEN = os.environ['NOTION_TOKEN']
NOTION_TESTS_DB = os.environ['NOTION_TESTS_DB']

tests = json.load(sys.stdin)
created = 0
updated = 0
skipped = 0

for tc in tests:
    supa_id = tc['id']

    filter_payload = json.dumps({
        'filter': {
            'property': 'supabase_id',
            'rich_text': {'equals': supa_id}
        }
    })

    req = urllib.request.Request(
        f'https://api.notion.com/v1/databases/{NOTION_TESTS_DB}/query',
        data=filter_payload.encode(),
        headers={
            'Authorization': f'Bearer {NOTION_TOKEN}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json'
        },
        method='POST'
    )

    try:
        resp = urllib.request.urlopen(req)
        query_result = json.loads(resp.read())
        existing_pages = query_result.get('results', [])
    except Exception as e:
        print(f'  ERROR querying Notion for test {supa_id}: {e}')
        skipped += 1
        continue

    def text_prop(val):
        if val is None:
            return {'rich_text': []}
        return {'rich_text': [{'text': {'content': str(val)[:2000]}}]}

    def title_prop(val):
        return {'title': [{'text': {'content': str(val or 'Untitled')[:2000]}}]}

    def number_prop(val):
        return {'number': val}

    def select_prop(val):
        if val is None:
            return {'select': None}
        return {'select': {'name': str(val)}}

    def date_prop(val):
        if val is None:
            return {'date': None}
        return {'date': {'start': val}}

    properties = {
        'title': title_prop(tc.get('title')),
        'supabase_id': text_prop(supa_id),
        'test_number': number_prop(tc.get('test_number')),
        'category': select_prop(tc.get('category')),
        'page_route': text_prop(tc.get('page_route')),
        'priority': select_prop(tc.get('priority')),
        'unit_status': select_prop(tc.get('unit_status')),
        'e2e_status': select_prop(tc.get('e2e_status')),
        'user_status': select_prop(tc.get('user_status')),
        'repo': text_prop(tc.get('repo')),
        'description': text_prop(tc.get('description')),
        'user_tester': text_prop(tc.get('user_tester')),
        'created_by': text_prop(tc.get('created_by')),
        'created_at': date_prop(tc.get('created_at')),
        'updated_at': date_prop(tc.get('updated_at')),
    }

    if existing_pages:
        page_id = existing_pages[0]['id']
        payload = json.dumps({'properties': properties})

        if DRY_RUN:
            print(f'  [DRY RUN] Would update test #{tc.get(\"test_number\")}: {tc.get(\"title\")[:50]}')
            updated += 1
            continue

        req2 = urllib.request.Request(
            f'https://api.notion.com/v1/pages/{page_id}',
            data=payload.encode(),
            headers={
                'Authorization': f'Bearer {NOTION_TOKEN}',
                'Notion-Version': '2022-06-28',
                'Content-Type': 'application/json'
            },
            method='PATCH'
        )
        try:
            urllib.request.urlopen(req2)
            updated += 1
            print(f'  Updated test #{tc.get(\"test_number\")}: {tc.get(\"title\")[:50]}')
        except Exception as e:
            print(f'  ERROR updating test #{tc.get(\"test_number\")}: {e}')
            skipped += 1
    else:
        payload = json.dumps({
            'parent': {'database_id': NOTION_TESTS_DB},
            'properties': properties
        })

        if DRY_RUN:
            print(f'  [DRY RUN] Would create test #{tc.get(\"test_number\")}: {tc.get(\"title\")[:50]}')
            created += 1
            continue

        req2 = urllib.request.Request(
            'https://api.notion.com/v1/pages',
            data=payload.encode(),
            headers={
                'Authorization': f'Bearer {NOTION_TOKEN}',
                'Notion-Version': '2022-06-28',
                'Content-Type': 'application/json'
            },
            method='POST'
        )
        try:
            urllib.request.urlopen(req2)
            created += 1
            print(f'  Created test #{tc.get(\"test_number\")}: {tc.get(\"title\")[:50]}')
        except Exception as e:
            print(f'  ERROR creating test #{tc.get(\"test_number\")}: {e}')
            skipped += 1

print(f'\\nTest cases sync complete: {created} created, {updated} updated, {skipped} skipped')
" DRY_RUN_PY="$DRY_RUN"
}

# ─── Main ──────────────────────────────────────────────────────────────────

echo "Notion Bug Sync — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Supabase: ${SUPA_URL}"
echo "Dry run: ${DRY_RUN}"
echo ""

case "$SYNC_TARGET" in
  bugs)
    sync_bugs
    ;;
  test_cases)
    sync_test_cases
    ;;
  all)
    sync_bugs
    echo ""
    sync_test_cases
    ;;
  *)
    echo "Unknown target: ${SYNC_TARGET}"
    echo "Usage: $0 [bugs|test_cases|all|--dry-run]"
    exit 1
    ;;
esac

echo ""
echo "Done."
