# The local job board (JobFlow)

The board is a small web app served from your own machine. It is where postings arrive,
where you decide which ones are worth your time, and where applications are drafted,
compiled and sent. Everything the slash commands do from the terminal can be started here.

```bash
python3 tools/jobs_board.py                 # opens http://127.0.0.1:8765/?t=<token>
python3 tools/jobs_board.py --port 9000 --no-open
python3 tools/jobs_board.py --new-token     # rotate the access token
```

The board uses only the Python standard library and has no build step. It binds to
`127.0.0.1` only, and every API call must carry the random token in the URL, so another
page open in your browser cannot drive it. The token lives in `job_scraper/.board-token`
(mode 0600, gitignored) and survives restarts, so a bookmarked URL keeps working.

It has four views: **Board**, **LinkedIn Inbox**, **Companies** and **Applications**.

## Board: collect and triage

**Fetch new jobs** runs every enabled source once, as one bounded run with one log.
Under the hood this is `tools/fetch_jobs.py`, which you can also run from a terminal:

| Source | What it reads | Notes |
|---|---|---|
| `ats` | Your companies' own boards (Greenhouse, Ashby, Lever, Personio, Workday; SmartRecruiters opt-in) | Only the companies in `job_scraper/companies.json`. Registry and vendor details: [`ats-search/SKILL.md`](.agents/skills/ats-search/SKILL.md) |
| `linkedin` | LinkedIn's public guest job search | Personal use only. Keep volume low; `detail` fetches are capped per run |
| `freehire` | freehire.me public API | Structured facets, e.g. `--country DE,CH,AT` |
| `arbeitnow` | Published Arbeitnow job-board API | Bounded pages, local geography/title filters and full descriptions |

LinkedIn searches use date order and retain a seven-day lookback. Queries and their
optional pages rotate within a shared 12-call keyword-search budget. Jobs already on
the board with missing LinkedIn descriptions remain eligible for bounded backfill.
Source failures and deferred work appear in the fetch summary; a failed query does
not count as a successful empty search. Configuration is local in
`job_scraper/scrape_config.json` (copy its `.example.json` twin for a new checkout).

Every new row passes through the same merge. That step:

1. deduplicates by canonical URL;
2. stores the posting body in `job_scraper/postings/`;
3. runs the **German screen**: a posting that states German as a job condition becomes
   `gate` with the sentence quoted (a passing mention is left for you to judge);
4. computes a **fit band** with `tools/fit_score.py`. This is a deterministic reading-order
   prior built from role family, skills, location, company affinity and freshness. It
   decides the order you read in and never drops a row. `python3 tools/fit_score.py --explain <url>`
   shows why a row scored what it did. `/rank` overrides it with an LLM assessment.

**Statuses** (`tools/jobs_md.py` is the source of truth):

| Status | Meaning |
|---|---|
| `new` | arrived in the latest fetch |
| `backlog` | unreviewed, from an earlier fetch (each fetch demotes `new` to here) |
| `yes` | worth applying to; the row goes to **Draft** |
| `applied` | application sent |
| `gate` | excluded automatically (language, deadline, …), still browsable |
| `no` | not for you |
| `expired` | posting is gone |

Filters cover source, fit band and when a row was found. Keyboard shortcuts are listed in the page.

### Changing a posting link

In the **Job** panel, click **Change URL** below the job title and source badges.
Paste the company's job or application page and click **Save URL**. The board's
**open posting** link and the application's **Open posting** link will use it
from then on. Your job status, notes and application history stay attached to
the same job. **Use original link** restores the automatically selected source link.

### Adding a job you found yourself

- **Add job** (in the board) takes a job URL, optionally the application URL and a
  description. It resolves the posting without a model: LinkedIn through the public job
  page, ATS links through `ats-search detail`, anything else through a plain fetch.
- **Bookmarklet:** drag **`+ JobFlow`** from the Add job dialog to your bookmarks bar.
  Clicking it on any job page opens the dialog already filled in.
- **LinkedIn "recommended for you"** is only visible when you are signed in. Use the
  LinkedIn inbox's capture flow below to import rendered cards in a batch. The
  `linkedin-browser-import` skill remains an interactive fallback when capture fails.

### LinkedIn inbox and optional automatic intake

The LinkedIn inbox stores captured cards separately from the board. Capture adds no
job application or account action. Processing reads guest posting details, records
external apply URLs when available, screens requirements, and imports through the
existing merge. Failed details remain retryable; jobs with incomplete evidence are
not presented as screened survivors. Import and deterministic fit scoring use no LLM.

Use **Capture LinkedIn list** on a signed-in LinkedIn Jobs page. It captures currently
rendered cards, then opens a local preview. Confirm the preview to add cards. Scroll
and repeat to capture more; repeated IDs are deduplicated. Paste captured card JSON
into the same preview if a bookmarklet is blocked. Click **Process inbox** to import
a bounded batch. Same company/title is a possible duplicate, not an exact-ID match.

LinkedIn prohibits scripts that copy or automate its service. Manual triggering and
small budgets do not create permission. The board never reads cookies, exports your
session, or automates signed-in navigation. [LinkedIn policy](https://www.linkedin.com/help/linkedin/answer/a1341387/).

For automatic intake, configure either or both options in the local scrape config:

- `linkedin_intake.email`: set `enabled: true`, choose `directory` (default
  `job_scraper/linkedin_emails`) and `max_messages`. Have your mail client export or
  deliver job-alert/recommendation messages there as `.eml` files. The collector reads
  that directory only, ignores attachments and symlinks, and deduplicates message
  content and job IDs. This does not connect to a mailbox. It covers emailed jobs,
  which may differ from the recommendations on the website.
- `linkedin_intake.saved_jobs`: eligible Switzerland/EEA members can provision the
  official Member Portability API and authorize their own token. Set `enabled: true`
  and supply the token through the environment variable named by `token_env`
  (default `LINKEDIN_PORTABILITY_TOKEN`). Never put the token in scrape config. This
  fetches `SAVED_JOBS` only, in bounded resumable pages; it does not expose the
  recommendation list. Account access, response fields and snapshot freshness need
  verification after setup. [Official setup](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-member/).

Set `linkedin_inbox.auto_process: true` and `process_limit: 5` to process a small
inbox batch during Fetch or scheduled collection. Inbox details share the existing
LinkedIn detail budget. Both intake options are disabled in the public example until
configured. Inbox, message files, checkpoints and credentials stay local.

Preview optional intake from the terminal:

```bash
python3 tools/linkedin_intake.py --email-dir job_scraper/linkedin_emails --dry-run
python3 tools/linkedin_intake.py --saved-jobs --dry-run
```

Check for an existing job-collection LaunchAgent before installing another schedule;
keep one scheduled trigger for this pipeline to avoid duplicate fetches. Source and
budget changes apply the next time the existing schedule invokes the collector.

To enable daily macOS collection when no collector schedule is installed, customize and install
`tools/launchd/com.aijobsearch.scrape.plist` using the commands in that file. It runs
`tools/fetch_jobs.py`, with the same sources, budgets and locks as the board. If using
the saved-job API, ensure the token is available to the scheduled process through
your own secure environment setup; launchd does not inherit an interactive shell's
exported variables. The build does not install a scheduler or provision OAuth access.

## Companies: watch your target list

Paste a company's careers page URL. The board finds which ATS it uses and then checks that
board on every fetch. Detection that fails is retried on later fetches (backoff 1 / 3 / 7
days), and then the company moves to **Needs you** so you can supply the board URL yourself.
Companies are grouped into **Needs you**, **Finding** and **Watching**, with *Can't watch
yet*, *Not watched* and *Paused* folded away.

Use **Follow company** on a job to reuse an existing registry entry or add its employer.
Supply a careers page if the posting has no recognized ATS or employer careers link.
Aggregator posting URLs are not used as employer domains. Automatically extracted ATS
links are candidates until identity is verified; they are never labeled human-confirmed.
The Add company bookmarklet opens a local preview for explicit confirmation before
adding a careers page and resolving its board.

Pages that are not on a supported ATS are not watched. That is a deliberate scope limit,
not a missing feature.

## Applications: draft, review, send

Starting an application from a `yes` row runs the staged `/apply` pipeline as a
supervised background run:

1. **Prepare**: save the complete posting once, without a model where possible.
2. **Write and check**: write the requirement brief, pick the CV variant (`sde` or `ai`,
   used unchanged) and tailor a copy of the matching cover-letter base.
3. **Build and verify**: compile the CV with pdfLaTeX and the letter with XeLaTeX from
   `cover_letters/`, each exactly one page; check placeholders, contact details, the PDF
   text layer and keyword coverage on the CV itself.

Then the application moves through **Review** (a preview of the exact PDFs) and **Send**.
In Send you save the portal link you applied through and any notes, and mark the row applied.

Automated model review and visual inspection are **off by default** (`automated_review:
false`): you check the PDFs yourself in Review. Turn them on in `board_config.json` if you
want the reviewer / fix / layout-repair loop back.

**Continue** resumes an interrupted or failed run. It reuses every artifact that still
matches its checkpoint hashes and redoes only what is missing, even after a board restart.
**Regenerate** starts over from the saved posting and keeps earlier versions. Deleted runs
go to a trash, and their cost still counts against your budgets.

### Choose an engine

The **Engine** selector offers **Claude Code** (the default) and **Codex** for
generation, revision, regeneration, and Continue. A failed Claude attempt can
continue with Codex using its valid checkpoints. The engine is fixed when a run
is queued; changing it creates a linked attempt. There is no automatic fallback.

Codex requires a current CLI and an existing ChatGPT login (`codex login`). JobFlow
does not switch to API-key billing. Both engines use the same application rules,
evidence and templates. Codex receives input snapshots and returns structured
output; the supervisor validates and writes it. Shell, MCP, plugins and hooks are
disabled for these Codex passes. Optional visual inspection attaches rendered PDF
pages and requires `pdftoppm`; the owner's checklist remains manual.

Optional local settings in `job_scraper/board_config.json`:

```json
{
  "provider": "claude",
  "codex_bin": "codex",
  "codex_model": null,
  "codex_max_passes": 12
}
```

`codex_model: null` uses the CLI default; `claude_model` can also be set explicitly.
Codex account usage is recorded as tokens, with dollar cost **unknown**, not zero.
Claude's dollar budgets remain in force for Claude. Codex uses the shared queue,
timeouts and an attempt-level pass limit; these are not a dollar cap or a promise
about remaining subscription allowance. User CLI configuration is not loaded for
Codex pipeline passes; set the model in the board configuration instead.

Run the anonymous provider regressions with
`python3 -m unittest tests.test_codex_provider tests.test_provider_ui`.
The opt-in real CLI contract uses the signed-in allowance:
`JOBFLOW_LIVE_CODEX=1 python3 -m unittest tests.test_live_codex_contract`.

### Safety and budgets

Each Claude pass is a fresh, short session. It gets only the files it needs and a
write allowlist enforced by a hook (`tools/board/guard_write.py`). Your CV master and
cover-letter bases can never be written, through any path alias.

Budgets are runaway guards. The defaults are in `tools/board/run_registry.py`, and you can
override them in the gitignored `job_scraper/board_config.json`:

```json
{
  "daily_budget_usd": 10.0,
  "session_budget_usd": 12.0,
  "automated_review": false,
  "claude_bin": "claude"
}
```

`session_budget_usd` is cumulative across all attempts of one application, so retrying
does not get around it.

Each Claude pass uses the smaller of its configured stage cap and the attempt's remaining
reservation, rounded down to cents. This lets a later pass use the budget still available
instead of stopping because its full stage cap would exceed the reservation. When less
than $0.01 remains, the next pass is stopped before model work starts. Daily and cumulative
application caps still apply. If earlier passes used the model, the failure card says
"no model work started for this stage" rather than implying the whole attempt was unused.

## Notion as the status source of truth (optional)

With `/notion-sync` set up (the token goes in the gitignored `job_scraper/notion_sync.json`):

- publishing documents pushes the application row (title, company, URL, country, source)
  to your Notion "Job Applications" database;
- you set **Stage** in Notion;
- the board pulls Stage into `job_search_tracker.csv` at start and every ten minutes.

`python3 tools/notion_sync.py check|pull|push` does the same from a terminal. Only document
filenames are synced, never the documents themselves.

## Where the state lives

All of it is local and gitignored:

| Path | Contents |
|---|---|
| `job_scraper/seen_jobs.json` | every posting row and its status (canonical state) |
| `job_scraper/postings/` | stored posting bodies |
| `job_scraper/companies.json` | the watch list and your notes on each company |
| `job_scraper/runs.json`, `run_state/` | application runs, costs, guard logs |
| `documents/runs/` | per-run posting, drafts and model transcripts |
| `job_scraper/activity.jsonl` | the activity log behind the status strip |
| `job_search_tracker.csv` | application tracker (a Notion cache when sync is on) |
