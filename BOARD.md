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

It has three views: **Board**, **Companies** and **Applications**.

## Board: collect and triage

**Fetch new jobs** runs every enabled source once, as one bounded run with one log.
Under the hood this is `tools/fetch_jobs.py`, which you can also run from a terminal:

| Source | What it reads | Notes |
|---|---|---|
| `ats` | Your companies' own boards (Greenhouse, Ashby, Lever, Personio, Workday; SmartRecruiters opt-in) | Only the companies in `job_scraper/companies.json`. Registry and vendor details: [`ats-search/SKILL.md`](.agents/skills/ats-search/SKILL.md) |
| `linkedin` | LinkedIn's public guest job search | Personal use only. Keep volume low; `detail` fetches are capped per run |
| `freehire` | freehire.me public API | Structured facets, e.g. `--country DE,CH,AT` |

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
- **LinkedIn "recommended for you"** is only visible when you are signed in. Ask Claude
  Code to run the `linkedin-browser-import` skill with Claude in Chrome. It reads the list
  as text, opens only jobs the board does not already have (25 detail pages at most by
  default), and imports them through `tools/import_linkedin_browser.py`. It never handles
  your LinkedIn credentials, and never applies, messages or saves anything on LinkedIn.

## Companies: watch your target list

Paste a company's careers page URL. The board finds which ATS it uses and then checks that
board on every fetch. Detection that fails is retried on later fetches (backoff 1 / 3 / 7
days), and then the company moves to **Needs you** so you can supply the board URL yourself.
Companies are grouped into **Needs you**, **Finding** and **Watching**, with *Can't watch
yet*, *Not watched* and *Paused* folded away.

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

### Safety and budgets

Each model pass is a fresh, short `claude` session. It gets only the files it needs and a
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

Each model pass uses the smaller of its configured stage cap and the attempt's remaining
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
