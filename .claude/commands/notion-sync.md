# /notion-sync - Sync Applications with the Notion Database

The owner's Notion database **"Job Applications"** is the single source of truth for every application's lifecycle. `job_search_tracker.csv` is a **local cache** of it, kept so `/scrape`, `/rank`, `/outcome` and the board can dedup and exclude without a network call.

The sync is deterministic code, not model work: `tools/board/notion.py` (logic) and `tools/notion_sync.py` (CLI). The board runs it on its own - this command is for a manual refresh, a backfill, or first-time setup.

## Field ownership (each field has exactly one writer)

| Notion property | Written by | When |
|-----------------|-----------|------|
| Position Title, Company, Job Posting URL, Country, Source, Active Status | repo (push) | on publish; **only while empty** |
| Stage | **owner** | by hand in Notion. Push sets `Interested` only when Stage is empty |
| Next Action | push writes `Apply - drafted: <files>`; pull clears that text once Stage moves on | owner's own text is never touched |
| Application Date | pull, when Stage leaves Interested and the date is empty | page's last-edited date |
| Follow-up Date | pull, Stage = Applied and empty | Application Date + 10 days |
| First Response Date | pull, Stage ∈ OA / Phone Screen / Onsite / Offer / Rejected and empty (not when Outcome Reason = No response) | page's last-edited date |
| Furthest Stage | pull | raised to the current Stage; `Screened Out` for a rejection with none set |
| Outcome Reason | owner (pull fills `Withdrew by me` for Withdrawn when empty) | |
| Days to Response | Notion formula | |
| Application Portal, My Notes | **owner** | on the board's Send step (writes Notion, then the tracker's `portal_url` / `my_notes`) or by hand in Notion. Pull copies a non-empty value into the tracker and fills an empty one from it; clear a value on the board, not in Notion |

Every automatic write fills an **empty** field only, so correcting a date by hand is permanent.

**Stage -> tracker status:** Applied -> `applied`; OA / Phone Screen / Onsite -> `interview`; Offer -> `offer`; Rejected -> `rejected` (`no_response` when Outcome Reason is "No response"); Withdrawn -> `withdrawn`. Interested / empty never reach the tracker - drafts are recorded by the publish stage itself. Notion rows with no tracker match (applications made outside the repo) are appended to the tracker so `/rank` stops re-suggesting them.

## When it runs

- **Push** - the board's publish stage, in the background, right after the tracker row is written. A sync failure is an activity-log warning, never a failed run.
- **Pull** - when the board starts, then every 10 minutes while it runs.
- **By hand** - this command.

## Steps

1. Parse `$ARGUMENTS`: nothing or `pull` -> pull; `push` -> backfill every tracker row that has documents; `check` -> connection test.
2. Run `python3 tools/notion_sync.py <command>` and report its output in one or two lines.
3. **Exit 1 (not configured)** - walk the owner through setup once:
   1. Create an internal integration at <https://www.notion.so/profile/integrations>, copy its secret.
   2. In Notion, open the Job Applications database -> `...` -> Connections -> add the integration.
   3. Write the gitignored `job_scraper/notion_sync.json`:
      ```json
      {"token": "<secret>", "data_source_id": "<data source id>"}
      ```
      Find the data source id with the Notion MCP `fetch` tool on the database (`collection://<id>`), or leave it for the owner to paste. Never commit this file; `NOTION_TOKEN` in the environment overrides the token.
   4. Run `python3 tools/notion_sync.py check`, then `push` once to backfill.
4. **Exit 2 (API error)** - show the error. A 401/403 means the integration token is wrong or the database is not shared with it; a 404 on the data source means the id is wrong. Do not retry in a loop.

## Rules

1. **Never overwrite a non-empty Notion field** from the repo, and never set Stage beyond `Interested` - Stage is the owner's.
2. **Never write Stage from the tracker.** Status flows Notion -> tracker only; editing the tracker's status by hand is overwritten on the next pull.
3. **Documents stay local.** Only filenames reach Notion (in Next Action); CV and cover-letter contents never do.
4. **Match on Job Posting URL** (host + path, query stripped), then company + title. Never fuzzy-match.
5. **Never delete or archive Notion pages.**
