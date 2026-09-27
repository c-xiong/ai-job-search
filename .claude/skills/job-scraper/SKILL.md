---
name: scrape
description: >
  Finds new job postings matching your profile via installed portal-search CLIs
  (LinkedIn, local job boards, and any skills added with /add-portal). Deduplicates
  across runs. Triggers on: job scrape, find jobs, search jobs, new jobs, job search,
  scrape jobs, /scrape
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(bun --version), Bash(bun run .agents/skills/*/cli/src/cli.ts *), WebFetch, WebSearch, Agent, AskUserQuestion
---

# Job Scraper

---

## How It Works

This skill searches job portals using the **installed portal-search CLIs** in
`.agents/skills/` (plus WebSearch as a fallback), using queries from your profile.
It deduplicates against previously seen jobs and the application tracker, and
presents new matches with a quick fit assessment.

Authenticated LinkedIn job recommendations are an interactive source handled by Codex/ChatGPT App through `.agents/skills/linkedin-browser-import/`; they still enter the unified merge and board-generation workflow.

## Invocation

The user triggers this skill by saying things like:
- "Find new jobs"
- "Scrape for jobs"
- "Any new positions?"
- "/scrape"

Optional arguments:
- A focus area, e.g. "/scrape data science" or "/scrape geophysics"
- "broad" to run all search categories, e.g. "/scrape broad"
- "health" to run the portal health check only (Step 4.75), without searching, deduplicating, or presenting jobs - e.g. "/scrape health", or "/scrape health jobnet" to probe one portal even if disabled

---

## Execution Steps

### Step 0: Load State

1. Read `job_scraper/seen_jobs.json` (create if missing - start with `{"seen": {}}`). This is
   the sole job-state source; statuses and notes are edited only through the local board.
2. If `job_scraper/notion_sync.json` exists, run `python3 tools/notion_sync.py pull` (Notion is the source of truth; the tracker is its cache; a failure is one warning line). Read `job_search_tracker.csv` to extract already-applied companies+roles.
3. Read `search-queries.md` (this directory) for the search strategy.

### Step 1: Search

Read `search-queries.md` (this directory) for the search strategy. By default, run the top 3 priority query categories. If the user said "broad", run all categories. If the user specified a focus area (e.g. "data science"), prioritize queries from that category.

#### Binding run budget

The search should be broad enough to find work, but one invocation must never grow
with the number of installed portals, queries, or hits without a ceiling. Unless the
user explicitly supplies a different numeric budget, every normal, focused, and `broad`
run uses these hard limits:

- **12 keyword-search calls total** across all portals, plus the one permitted
  whole-board `ats-search` call. WebSearch fallbacks and health probes count against
  the same 12-call budget; a failed CLI does not create a free extra search.
- **15 results per keyword-search call.** An explicitly requested `broad` run may use
  20 per call, but does not lift any other ceiling.
- **60 deduplicated, previously unseen candidates** may enter Steps 2 and 3. Select
  them using target-company priority, title relevance, and recency; report the number
  deferred. Deferred candidates are not marked seen, so a later focused run can still
  evaluate them.
- **15 `detail` / WebFetch calls total**, after deduplication and only for candidates
  likely to be high or medium fit. Inline descriptions cost no extra request but do
  not lift the 60-candidate assessment ceiling.
- **30 jobs presented maximum.** Store assessed overflow normally, but keep the user
  report compact and state how many additional new rows were stored.
- **At most 3 Agent tasks**, partitioned by portal batch. Never spawn an Agent per
  query or per posting.

Reaching a ceiling is a normal bounded result, not a failure: stop that class of work,
keep completed results, and report `budget reached` with used/deferred counts. Only an
explicit numeric instruction from the user may change these limits; the word `broad`
alone never means unbounded.

**Use the installed CLI tools as the primary search mechanism.** Fall back to `WebSearch` only for portals that do not have a CLI skill, or if `bun` is unavailable on the system.

#### 1a. Check bun availability

```bash
bun --version
```

If this fails (bun not installed), skip to **1c (WebSearch fallback)** for all portals and note the fallback in the Step 5 output.

#### 1b. Run CLI tools (primary — run these in parallel where possible)

Discover all installed portal CLI skills by reading every `SKILL.md` found under `.agents/skills/*/SKILL.md`. Each file documents that portal's exact CLI flags and usage examples. **Use each portal's own documented interface — do not guess flags.** This approach automatically includes any new portals added via `/add-portal` without requiring changes to this file.

**Honor the `enabled` toggle.** A portal is enabled unless its `SKILL.md` frontmatter sets `enabled: false` (a missing key means enabled — the default). Skip each disabled portal and record it for the Step 5 summary. A fork can thus keep a portal installed but sit out a run without deleting its directory.

For each **enabled** portal skill:

1. Read its `SKILL.md` to find the correct `bun run …` invocation and supported flags.
2. Translate the query terms from `search-queries.md` into that portal's flag format (e.g. `--key`, `--search-string`, `--query`, filter codes — whatever the portal's SKILL.md specifies).
3. Scope to the last 14 days using the portal's supported recency flag (`--jobage`, `--since <YYYY-MM-DD>`, `--order PublicationDate`, etc. — as documented per portal).
4. Cap results to 15 per call (20 only for an explicitly requested `broad` run) using the portal's limit flag.
5. Use `--format json` for machine-readable output.

**A portal whose `SKILL.md` states a single-call rule gets exactly one invocation per run.** Not every source is keyword-driven: a whole-board source (`ats-search`) fetches complete job boards and filters locally, so translating each query category into its own invocation multiplies real HTTP requests by the number of categories and returns the same rows every time. Where the portal's docs say "call this once per run", step 2 above does not apply to it — pass its own selection flags instead, and use `-q` (if it has one) as a local filter on what came back.

Run all portal CLI calls in parallel where possible using the Agent tool. Collect all `results` arrays into a single pool for Step 2, keeping each result tagged with its source portal skill (for Step 2 `detail` lookups).

If a CLI tool exits with a non-zero code, log the error message and continue — do not abort the whole search.

#### 1c. WebSearch fallback

Use `WebSearch` for:
- Portals listed in `search-queries.md` that do **not** have a corresponding directory under `.agents/skills/`
- Any portal whose CLI fails at runtime
- When bun is unavailable (Step 1a failed)

Use the site-specific query strings from `search-queries.md` directly as WebSearch queries for these portals.

### Step 2: Fetch & Parse

For each promising result from Step 1:

**From CLI results:** Search output already includes title, company, location, date,
and URL. For jobs worth a deeper look, fetch full detail with that portal's `detail`
command (see its SKILL.md — do not guess flags) to extract **key requirements**,
**application deadline**, and a brief description snippet.

**From WebSearch results:** Use `WebFetch` on the posting URL and extract the same
fields manually. If it returns HTTP 403, retry with browser headers via curl per
`.claude/skills/job-application-assistant/09-web-research.md` before giving up — most
bank and corporate sites reject WebFetch's user agent while serving browsers normally.

**Store a URL that actually resolves to the posting.** A listing-page URL with a
`#fragment` appended (`.../jobs/ciso/#ikerian`) is not a posting: it fetches fine and
returns unrelated job titles, which makes every later `/rank` and `/apply` run fail on
that entry. When WebSearch only yields a listing page, search the employer's own careers
site for the role and store that URL instead, or drop the candidate rather than saving a
fragment link.

For every candidate:
- Skip if the URL or company+title combo already exists in `seen_jobs.json`
- Skip if the company+role already appears in `job_search_tracker.csv`

### Step 2.5: Mass-Posting Detection (within this run)

A distribution pattern worth flagging to the user as a caution signal, not as an accusation against the employer - it describes how a listing is being distributed, not a verdict on whether the company is legitimate. It alone proves nothing is wrong (companies do legitimately hire the same role across several cities); flag it so the user can factor it in when deciding whether to invest time, don't downgrade fit or silently exclude the result because of it.

If two or more results in this run's pool (from the same company, or sharing the same req/job ID visible in the URL or title) have substantially the same description and differ only in city/location/title, don't present them as separate rows. Consolidate into a single row and note the spread, e.g. "posted identically across 6 cities (BR, MX, GT)".

### Step 3: Fit Assessment — computed, not judged

**Do not assign `fit` by hand, and never write the field yourself.** The band is
computed in code by `tools/fit_score.py` at merge time, for every row from every
source, and it costs no tokens. Writing a hand-picked `high`/`medium`/`low` over
it produces a column where two rows with the same label were decided by
different rules, and the board sorts on the number behind that label.

What the scorer does, so you can explain a row without re-deriving it:

| component | max | reads |
|---|---|---|
| role family | 25 | title first, body only if the title names nothing |
| seniority fit | 25 | full ladder from the title; years-of-experience regex from the body |
| skill overlap | 18 | the stored posting body; additive only, saturates fast |
| company affinity | 17 | your hand-set 0-5 rating in `job_scraper/company_affinity.json` |
| location | 15 | city tiers; unresolved is neutral, never zero |

Bands: **high ≥ 75, medium 58-74, low < 58**, all tunable in
`job_scraper/fit_profile.json`. Four gates can only ever *lower* a band, never
raise it, and each records its reason: German stated as a job condition, a title
that is not an engineering or AI role at all, a stack on the exclude list, and -
the one that bites most often - **no stored posting text caps the row at
`medium`**, because a row with no evidence must not claim a strong match.

Your job in this step is therefore to *report*, not to score:

- If a row still shows no band, the profile failed to load. Say so; do not
  substitute a guess.
- **Language:** the German screen already runs in code
  (`collectors.german_hit`) and files a hard requirement as `gate`. For any
  *other* undeclared language from `04-job-evaluation.md`'s Language Gate,
  raise it in the Step 5 highlights and recommend the user set the row to `no` -
  the scorer does not read languages beyond German. A **declared** language at a
  level above the user's is never an auto-downgrade: quote the requirement next
  to the declared level in the highlights so the gap is visible.
- To re-score after tuning the profile: `python3 tools/fit_score.py --recompute`
  (add `--dry-run` first; it prints a before/after band histogram).
- To see why one row scored what it did: `python3 tools/fit_score.py --explain <url>`.

### Step 4: Deduplicate & Store

1. Add ALL fetched jobs (new and skipped) to `seen_jobs.json` with structure:
```json
{
  "seen": {
    "<url_or_company_title_key>": {
      "title": "...",
      "company": "...",
      "url": "...",
      "first_seen": "YYYY-MM-DD",
      "deadline": "YYYY-MM-DD" | null,
      "fit": "high/medium/low",
      "status": "new/skipped/ranked/expired",
      "portal": "<source portal skill, e.g. jobindex-search>"
    }
  }
}
```

**Computed fields, written by code — never by hand.** `tools/postings.py` stores
the posting body in a sidecar and records `posting_path`, `posting_chars`,
`posting_excerpt`, `posting_source`, `posting_extractor` and
`posting_fingerprint` on the row. `tools/fit_score.py` then writes `fit`,
`fit_score` (the raw 0-100), `fit_priority_score` (that number capped by what
the displayed band allows, which is what the board sorts on), `fit_parts` (the
five components), `fit_reasons`, `fit_source`, `fit_evidence` and `fit_version`.

The body itself is **not** in `seen_jobs.json`: that file is rewritten in full on
every board click and shipped to the browser on every reload, so the bodies live
in `job_scraper/postings/<sha1>.txt` and the row keeps only pointers.

`fit_source` is the one that matters when writing: `"deterministic"` means the
scorer owns the band and may refresh it; `"ranked"` means `/rank` owns it and
neither a collect nor a recompute may move it.

The `portal` field records which CLI skill produced the job (results are already tagged per portal in Step 1b - persist that tag here). Entries written before this field existed lack it; the health check (Step 4.75) attributes those by matching the URL's domain against each portal's base URL, so do not backfill.

`/rank` extends this schema additively: ranked entries also carry `rank_score` (0–100 overall score), `rank_verdict` (fit band, e.g. "strong fit"), `rank_date` (ISO date of ranking), and `strengths`/`gaps` (1-3 verbatim bullets each, copied from the scoring agent's findings). The `status` field is set to `"ranked"`. Do not drop any of these fields when re-writing entries. Entries ranked before `strengths`/`gaps` existed simply lack them; readers tolerate their absence and never backfill by guessing.

**No implicit migration; explicit recomputation is fine.** The rule below is
about *guessing*: a reader tolerates a missing key and never invents a value for
it. It does not forbid a deliberate, repeatable, dry-runnable pass over the
board — `tools/fit_score.py --recompute` and the posting backfill are exactly
that, they announce what they changed, and `--dry-run` shows the effect first.

`deadline` is a base field rather than a `/rank` extension: Step 2's detail fetch already extracts the application deadline, so it is written when the job is first seen and refreshed by `/rank` Step 4 when a scoring agent returns a different value. `null` means the posting states no deadline; a missing key means the entry predates this field - **never infer a deadline** from either, and never backfill by guessing.

2. Only present jobs NOT already in the seen list or tracker.

3. **Respect the user's own exclusions.** An entry whose `user_status` is `no` was excluded by the
   user personally - never present it again and never re-score it. `yes` and `applied` are the
   user's, not yours: carry them through untouched. (`star` and `maybe` are retired - the board
   reads them as `yes` and `backlog` and rewrites them on the next save.) `new` means "arrived in the
   latest fetch"; a fetch demotes older unreviewed `new` rows to `backlog` before merging.

4. **Persist only the canonical state.** Write `job_scraper/seen_jobs.json`; the local board
   reads it directly and is the only editing interface. Do not generate Markdown or CSV during
   a scrape. `jobs.md` is an optional read-only snapshot created only with
   `python3 tools/jobs_md.py export-md`; the two CSV snapshots are created independently with
   `python3 tools/jobs_md.py export-csv`. Neither export is ever read back into state.

### Step 4.5: Generate Referral Contact Links (High & Medium Fit Only)

For every job from this run with `fit` of **high** or **medium** (skip low-fit jobs),
build two LinkedIn people-search URLs so the user can find a recruiter or team member to
reach out to for a referral or a warm intro. This is deliberately a link-generation step,
not an automated lookup: no scraping, no third-party API, zero runtime dependencies or
credentials required.

**A. Recruiters / Talent Acquisition (the referral path)**
```
https://www.linkedin.com/search/results/people/?keywords=<url-encoded "<Company Name> recruiter">&origin=GLOBAL_SEARCH_HEADER
```

**B. Role/team peers (informational-outreach / warm-intro path)**
```
https://www.linkedin.com/search/results/people/?keywords=<url-encoded "<Company Name> <role keyword>">&origin=GLOBAL_SEARCH_HEADER
```
Use a short keyword drawn from the posting's title for `<role keyword>` - e.g. a posting
titled "AI Program Manager" becomes `"<Company Name> AI Program Manager"`.

Both links are for the user to open and browse themselves - never fetch or scrape the
LinkedIn people-search result pages programmatically. Never fabricate contacts or claim a
specific person was found; these are search links, not results.

### Step 4.75: Portal Health Check

Scraper-based portal CLIs rot silently: when a portal changes its markup, the parser usually exits 0 with zero results or with null/garbled fields, and the Step 1c fallback never fires because it only triggers on hard failure. This step catches that from evidence the run already holds.

**Free pass (no extra requests).** For each enabled portal that ran in Step 1b:

- **Degraded scan:** inspect the results it returned this run. Flags: `company` null or empty on every result, empty titles, undecoded entities (`&amp;`) or HTML fragments in titles, URLs that do not point at the portal. Any of these means the parser is half-working and `/scrape` is silently collecting junk.
- **Yield history:** if the portal returned zero results across all of this run's queries, check whether `seen_jobs.json` holds prior entries from it (via the `portal` field, or by matching URL domains for entries predating the field). A portal that produced jobs on earlier runs and produces nothing now is suspect - the same queries worked before.

**Escalation (bounded, on suspicion only).** A suspect portal gets **one** sentinel probe: run its documented `search` with the example query from its own SKILL.md (that query provably worked when the skill was registered), the portal's limit flag capped at 3, `--format json`. If that returns nothing, retry **once** with a single common word. Only then is the verdict **broken**. A 429 or block page is **never** evidence of breakage - record the portal as **inconclusive (rate-limited)**, back off, and do not retry.

**Verdicts.** Healthy portals get silence - no table, no line. Anything else surfaces in the Step 5 summary as a health line.

**Probe-only mode (`/scrape health`).** Skip Steps 1-4 and this step's free pass (there is no fresh run to scan); instead probe every installed portal directly - enabled ones by default, a disabled one only when named explicitly (e.g. `/scrape health jobnet`). Each portal gets the sentinel probe above, the degraded criteria applied to whatever it returns, and - since the user explicitly asked for diagnosis - one `detail` fetch on the first result of each healthy portal (description must be readable decoded text; a failure downgrades to degraded). Report all statuses in this mode, including healthy. Volume stays bounded: one search, at most one retry, at most one detail per portal.

### Step 5: Present Results

Present new jobs in a table sorted by fit (high first). When Step 1b skipped
portals (`enabled: false`), report them with the `skipped (disabled):` line below
so opting one out stays visible rather than silent; omit the line when nothing
was skipped. When Step 4.75 found a portal degraded, broken, or inconclusive,
add one `health:` line per suspect portal (healthy portals get no line); after
the report, offer to set that portal's `enabled: false` so `/scrape` stops
running it (and covers it via the Step 1c fallback) until it is fixed - only
edit the toggle with the user's confirmation, and never edit anything else in
the skill.

```
## New Job Matches - YYYY-MM-DD

Found X new positions (Y high, Z medium, W low match).

skipped (disabled): <portal-name>, <portal-name>

health: <portal-name> - degraded (company null on all 12 results); parsing anchors in .agents/skills/<portal-name>/url-reference.md
health: <portal-name> - broken (0 results for the SKILL.md test query and a broader retry); parsing anchors in .agents/skills/<portal-name>/url-reference.md

| # | Fit | Title | Company | Location | Deadline | URL |
|---|-----|-------|---------|----------|----------|-----|
| 1 | High | ... | ... | ... | ... | [Link](...) |

If Step 2.5 flagged a mass-posting pattern, note it in the Title cell (e.g. "Frontend Developer (posted in 6 cities)") rather than burying it. Do the same for a declared-language-insufficient-level flag from the Language Gate (e.g. "Backend Engineer ⚠ fluent English required") - both are signals the user should see at a glance, not just in the detail highlights below.

### High-Match Highlights
For each high-match job, add 2-3 bullet points:
- Why it matches your profile
- Key requirements to check
- Any red flags (including mass-posting signals from Step 2.5)

### Contacts
For each high/medium-fit job from Step 4.5, add a short contacts block with the two
LinkedIn search links:
- Recruiters/TA search link, for the referral path
- Role/team-peer search link, for the warm-intro / informational-outreach path
```

After presenting, ask:
> "Want me to evaluate any of these in detail? Just give me the number(s)."

If the user picks a number, invoke the **job-application-assistant** skill workflow (posting -> requested documents; a fit evaluation only if the user asks for one).

If the run found many new jobs (roughly 8+), also suggest `/rank` - it batch-scores all new postings against the full fit framework and returns a ranked shortlist, which beats eyeballing a long table. (`/rank` sets the `ranked` and `expired` status values in `seen_jobs.json`; treat both as already-seen for dedup purposes.)

### Step 6: Update Tracker (Optional)

If the user decides to apply to any job, the tracker row is written by **job-application-assistant Step 3b**, which Step 5 already routes into - do not add a second row here. Only when the user says they applied to something outside that path, add a row using the header and the match-then-update rule in `/outcome` Step 1.

---

## Important Rules

1. **Never fabricate job postings.** Only present jobs from actual CLI search/detail output or WebSearch/WebFetch results.
2. **Respect deduplication.** Always check seen_jobs.json AND job_search_tracker.csv before presenting.
3. **Focus on configured geographic area.** Skip jobs that require relocation or are clearly outside commute range.
4. **Only open positions.** Skip postings with expired deadlines or those marked as closed.
5. **Be efficient with detail fetches.** Don't run `detail` or WebFetch on every search hit — pre-filter by title/snippet, then fetch only promising matches.
6. **Parallel searches.** Run portal CLI searches in parallel; use WebSearch only for gaps the CLIs don't cover.
7. **No automated people lookups.** Referral contacts (Step 4.5) are LinkedIn search links only - never fetch or scrape LinkedIn people-search result pages programmatically.
8. **Health checks are bounded and honest.** Step 4.75 spends at most one probe, one retry, and (in `health` mode) one detail fetch per portal - a diagnosis, not a crawl. A rate-limit is never evidence of breakage. Health verdicts come only from observed CLI output; a portal that could not be tested is reported as inconclusive, never guessed. The `enabled` toggle is the only thing the health check may edit, and only with confirmation.
9. **Flag distribution patterns, never accuse.** The mass-posting signal (Step 2.5) describes how a listing is being distributed, not a claim that the employer is a scam. Never name a company as fraudulent or untrustworthy - present the observation and let the user decide.
