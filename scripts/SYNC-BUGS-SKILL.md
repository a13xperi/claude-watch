# sync-bugs — Mirror Supabase bug tracker to Notion

## What it does
Syncs `bugs` and `test_cases` tables from Supabase (project zoirudjyqfqvpxsrxepr) to Notion databases for human-readable browsing. Uses supabase_id as the dedup key -- safe to run repeatedly (creates on first sync, updates on subsequent runs).

## Prerequisites
1. **Notion integration token** -- create at https://www.notion.so/my-integrations
2. **Two Notion databases** -- one for bugs, one for test cases
3. Share both databases with the integration

### Notion database property setup

**Bugs DB:**
| Property | Type |
|---|---|
| title | title |
| supabase_id | rich_text |
| bug_number | number |
| severity | select (cosmetic, minor, major, critical) |
| status | select (open, in_progress, fixed, verified, wontfix) |
| page_route | rich_text |
| project | rich_text |
| source | rich_text |
| description | rich_text |
| steps_to_reproduce | rich_text |
| branch | rich_text |
| repo | rich_text |
| file_path | rich_text |
| found_by | rich_text |
| fixed_by | rich_text |
| pr_url | url |
| tags | multi_select |
| created_at | date |
| updated_at | date |
| found_at | date |
| fixed_at | date |
| verified_at | date |

**Test Cases DB:**
| Property | Type |
|---|---|
| title | title |
| supabase_id | rich_text |
| test_number | number |
| category | select (functional, visual, performance, accessibility) |
| page_route | rich_text |
| priority | select (critical, high, medium, low) |
| unit_status | select (pending, pass, fail, skip) |
| e2e_status | select (pending, pass, fail, skip) |
| user_status | select (pending, pass, fail, skip) |
| repo | rich_text |
| description | rich_text |
| user_tester | rich_text |
| created_by | rich_text |
| created_at | date |
| updated_at | date |

## Environment variables
```bash
export NOTION_TOKEN="ntn_XXXXXXX"
export NOTION_BUGS_DB="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export NOTION_TESTS_DB="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

## Usage
```bash
# Sync everything
~/projects/token-watch/scripts/sync-bugs-to-notion.sh

# Sync only bugs
~/projects/token-watch/scripts/sync-bugs-to-notion.sh bugs

# Sync only test cases
~/projects/token-watch/scripts/sync-bugs-to-notion.sh test_cases

# Dry run (shows what would sync without writing)
~/projects/token-watch/scripts/sync-bugs-to-notion.sh --dry-run
```

## How dedup works
Each Notion page has a `supabase_id` rich_text property. On sync, the script queries Notion for existing pages with that ID. If found, it updates; if not, it creates. This means the script is idempotent -- run it as often as you want.

## Automation
To run on a schedule, add to cron or use `/schedule` skill:
```bash
# Every 15 minutes
*/15 * * * * NOTION_TOKEN=xxx NOTION_BUGS_DB=xxx NOTION_TESTS_DB=xxx ~/projects/token-watch/scripts/sync-bugs-to-notion.sh >> /tmp/notion-sync.log 2>&1
```
