---
name: ats-search
version: 1.0.0
description: >
  Use this skill to check a hand-maintained list of target companies directly on
  their own applicant-tracking boards (Greenhouse, Ashby, Personio, Lever,
  SmartRecruiters) instead of going through a job aggregator. It is a
  target-company monitor, not a market search: it answers "what is open right now
  at the companies I care about", including roles that never reach LinkedIn.
  Trigger phrases: check my target companies, what's open at <company>, company
  career page jobs, ATS board, Greenhouse/Ashby/Lever/Personio/SmartRecruiters
  jobs, direct company jobs, first-party job listings, new roles at the companies
  I follow.
context: fork
enabled: true  # set to false to keep this source installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/ats-search/cli/src/cli.ts *)
---

# ATS Search Skill

Monitor **your** target companies on **their** boards. The companies live in
`job_scraper/companies.json`; the CLI reads that registry, fetches the boards
whose cadence is up, filters locally, and returns postings in the standard portal
shape. No API key, no account, zero runtime dependencies.

This is not market coverage and the docs must not imply it is. It sees exactly
the companies you put in the registry, on the five board vendors v1 supports.

## ⚠️ Read this before wiring it into anything: ONE call per run

`search` is a **whole-board** source, not a keyword portal. It takes **no
required query**: one invocation reads the registry, fetches each due company's
board once, and filters in process.

> **Call this CLI exactly once per run. Do not translate query categories into
> per-query invocations.** `-q` is a *local* filter over results already fetched -
> it never issues an extra request.

Two backstops exist because getting this wrong is expensive: `--max-companies`
bounds a run (default 8), and every company has a cooldown (`min_interval_minutes`,
default 60) inside which its board is served from the last run's cached payload.

## Commands

### `search` — the due companies, in one batch

```bash
bun run .agents/skills/ats-search/cli/src/cli.ts search --max-companies 8 --format table
```

| Flag | Meaning |
|---|---|
| `--query`, `-q <text>` | Local OR filter over title/company/location. **No extra requests.** |
| `--company`, `-c <name>` | Fetch these companies instead of the due ones. Repeatable. Takes a registry name, an alias, or `vendor:token`. |
| `--max-companies <n>` | Companies per run. Default 8. It bounds an explicit `--company` list too. |
| `--max-new-jobs <n>` | Stop selecting further companies once this many **new** rows have accumulated. Default 0 (unbounded). Not a truncation point: the company in flight is finished in full, then no further company is selected — a half-ingested company would leave a state where a later run cannot tell "not fetched" from "fetched and filtered out". |
| `--known-ids <file>` | Composite ids already in your board, one per line or a JSON array. This is what makes `--max-new-jobs` count *new* rows: without it a first company whose whole board you have already seen would spend the budget having added nothing, and defer the company that actually had new work. `tools/ats_fetch.py` writes this file for you. |
| `--limit`, `-n <n>` | Cap rows after sorting. Default 0 = no cap. |
| `--jobage <days>` | Drop postings older than N days. A posting with **no date is kept**. |
| `--page <n>` | Accepted for contract compatibility. The whole board is already in hand, so only page 1 carries results; `--page 2` returns an empty set with a `meta.note`, never an error. |
| `--format json\|table\|plain` | Default `json`. |
| `--force-refresh` | Ignore the per-company cooldown. Manual debugging only. |
| `--no-write` | Do not write fetch bookkeeping back into the registry. |

**Naming a company does not lift a rule.** `--company` chooses *which* companies
to fetch. It cannot fetch a board whose identity is unverified (that is what
`resolve` is for), it cannot reach a vendor whose published policy said no, and
it is still bounded by `--max-companies`. A request that resolves to no fetchable
company exits **1** with `NO_FETCHABLE_COMPANY` — refusing to ask is not the same
as asking and finding nothing, and a caller must be able to tell them apart.

**Which companies a run picks.** Eligible = `route: ats` **and** `status: verified`
**and** `now - last_success_at >= cadence_days`. Eligible companies are ordered by
staleness, tie-broken by tier, and the first `--max-companies` are taken. That is
a tier-weighted staleness rotation: it is what "weekly" means without a scheduler,
and it makes every run bounded. Companies that do not fit keep their old
`last_success_at`, so the next run picks them first.

### `detail` — one posting

```bash
bun run .agents/skills/ats-search/cli/src/cli.ts detail ashby:deepjudge:41c79388-... --format plain
bun run .agents/skills/ats-search/cli/src/cli.ts detail https://jobs.lever.co/sonarsource/54743786-... --format plain
```

Four of the five vendors ship the description with the board listing, so a search
of 40 roles is 8 requests, not 48. **Do not loop `detail` over search hits** —
reach for it when you have an id from the tracker, or for a SmartRecruiters
posting (the one vendor whose listing carries no description).

### `resolve` — find a board, and prove it is theirs

```bash
bun run .agents/skills/ats-search/cli/src/cli.ts resolve --all-unresolved --dry-run
bun run .agents/skills/ats-search/cli/src/cli.ts resolve -c "DeepJudge"
```

Evidence-first: it reads the company's **own** careers page, identifies the ATS
from that page's markup, and probes only that vendor. Slug guessing is the
fallback, not the method.

### `companies` — read the registry, and close the loop

```bash
bun run .agents/skills/ats-search/cli/src/cli.ts companies --list
bun run .agents/skills/ats-search/cli/src/cli.ts companies --due       # what the next run would fetch
bun run .agents/skills/ats-search/cli/src/cli.ts companies --suggest   # starred employers missing from the registry
```

## What counts as "this board is really theirs"

A returned URL that echoes the token you asked for is **not** verification. Three
of the five vendors publish no company identifier at all, and the trap is real:
`unique.jobs.personio.de` is a live Personio board whose postings belong to
*unique land use GmbH* in Freiburg — not to Unique AG in Zurich. A slug that
answers 200 proves a board exists, not whose it is.

| Rank | `evidence_kind` | What it is | Where it exists |
|---|---|---|---|
| 1 | `company_site_link` | The company's own domain links to the board URL/token | any vendor |
| 2 | `vendor_identifier` | The payload carries a company identifier matching the registry name or an alias | greenhouse, smartrecruiters |
| 3 | `human_confirmed` | You looked and said yes, recorded with the date | any |
| — | *not evidence* | a URL echoing the requested token · a plausible country · a 200 status | — |

Four distinct not-yet-resolved states, kept apart on purpose:

| Status | Means | Next action |
|---|---|---|
| `unresolved` | No candidate board found | Retry after a rename, or resolve by hand |
| `ambiguous` | A candidate exists but identity evidence is insufficient, or several matched | Printed for your decision; **never** auto-promoted |
| `unsupported_vendor` | Their site confirms an ATS v1 has no adapter for | Adapter backlog; `route: linkedin` meanwhile |
| `no_public_board` | Confirmed **only after a human read the careers page** | `route: manual` |

"All my slug guesses failed" therefore resolves to `unresolved`, never
`no_public_board` — a custom domain, an uncovered ATS, a non-derivable token, an
empty board and a JS-rendered careers page all look identical from the outside.

## Vendors in v1

| Vendor | One call per board | Company identifier | Date field | Description inline? | Trap |
|---|---|---|---|---|---|
| greenhouse | `boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true` | `company_name` | `first_published` | yes | EU tenants answer on `job-boards.eu.greenhouse.io`; `updated_at` is a different fact and is never used as the posting date |
| ashby | `api.ashbyhq.com/posting-api/job-board/{t}` | none | `publishedAt` | yes (`descriptionPlain`) | `isListed` must be honored; `addressCountry` is a country *name*; big payloads |
| personio | `{t}.jobs.personio.de/xml` | none | `createdAt` | yes | non-customers answer **307**, not 404; the subdomain proves a subdomain exists, not whose it is |
| lever | `api.lever.co/v0/postings/{t}?mode=json` | none | `createdAt` (**epoch ms**) | yes | bare JSON array |
| smartrecruiters | `api.smartrecruiters.com/v1/companies/{t}/postings` | `company.identifier` | `releasedDate` | **no** | **200 + `totalFound: 0` for any unknown slug** — so a zero-result board is reported `not_found`, never `empty`. **Disabled by default**, see below |

### ⚠️ SmartRecruiters is off by default, on the vendor's own instruction

`api.smartrecruiters.com/robots.txt` reads

```
User-agent: LinkedInBot
Allow: /v1/companies/

User-agent: *
Disallow: /
```

Every agent except LinkedIn's is disallowed from the whole host, this endpoint
included (checked 2026-08-18 with `python3 tools/robots_check.py`). The adapter is
implemented and tested, but it is **fail-closed**: the policy lives in the CLI
(`registry.ts` `POLICY_BLOCKED`), not only in the registry, so deleting the
`defaults.vendor_access` block does **not** re-enable it, and naming the company
on the command line does not either. `detail` is gated on the same rule — it
reaches the same host.

Turning it on is `defaults.vendor_access.smartrecruiters.enabled: true`, and it is
a **personal-use decision at your own responsibility**: keep the volume tiny,
never bulk or commercial. `url-reference.md` records the verdict and the date for
every vendor host.

**Any host that answers 429 is stopped for the day**, with no retry — for every
command, persisted in `job_scraper/ats_cache/rate_limited.json` so the stop
survives the process. A 429 is never treated as evidence of breakage.

## Output

```jsonc
{
  "meta": {
    "count": 12, "page": 1,
    "degraded": false,               // true when some companies failed and others succeeded
    "status_counts": {"ok": 7, "not_found": 1},
    "companies": [ {"name","vendor","token","status","message","jobs_seen","eligible","requests","cached"} ],
    "requests": 8, "generated_at": "..."
  },
  "results": [ {"id","title","company","location","date","url", /* + vendor, token, tier,
     city, country, region, workplace, location_uncertain, deadline, description,
     prefit_score, prefit_reasons, flags */ } ]
}
```

`id` is composite — `<vendor>:<token>:<posting_id>` — because a bare vendor id is
not unique across ~55 companies and five vendors. A bare id passed to `detail` is
rejected with `AMBIGUOUS_ID` rather than guessed at.

**Exit codes.** 0 when at least one company succeeded (with `meta.degraded` when
some did not — the successes are always in `results`). 1 only when *every*
selected company failed (`ALL_COMPANIES_FAILED`, with `meta.companies` still on
stdout for diagnosis), or the registry is bad (`BAD_REGISTRY`), or the arguments
are (`BAD_ARGS` / `AMBIGUOUS_ID`). Errors go to stderr as `{"error","code"}`.

**`prefit_score`** (0–100) is a deterministic *display prior* so a fresh batch
sorts sensibly before `/rank` has run. It is **not** a fit assessment: it never
gates and never drops a posting, it always ships with `prefit_reasons`, and
`/rank` overwrites it as the authority.

## Notes and known limits

- **Access terms.** These are the vendors' own public board APIs — documented,
  keyless, and meant to be read by machines. That is not the same as unlimited
  permission: `url-reference.md` records the `robots.txt` verdict and terms link
  per vendor host, and the CLI keeps volume low by design (one call per board,
  250 ms spacing, per-host serialization, honest `ats-search-cli/1.0` UA,
  exponential backoff with jitter on 429/5xx).
- **A missing date stays missing.** No vendor date means `date: null` and the
  posting is *kept*; `first_seen` downstream is the discovery date. `now()` is
  never substituted for a posting date.
- **Geography is structured, not substring-matched.** `workplace` ×
  `allowed_countries` × `remote_regions`. "Remote (US)" is dropped; "Remote —
  Europe" is kept; a bare "Remote" is kept and flagged `location-uncertain` but
  does not qualify on its own.
- **Multi-location postings** qualify if any location qualifies, and the
  qualifying location is what gets reported. Each location keeps the country the
  vendor attached to *it*, not the posting's primary — an Ashby posting with a New
  York primary and a Berlin secondary is a Berlin option, not a US one.
- **A macro-region only qualifies a remote posting.** "EMEA" on an onsite role
  names a market, not a place you could commute to; it leaves the posting
  `location-uncertain` (surfaced and flagged) rather than qualifying it.
- **`cities` is a scoring signal, not a filter.** A posting in an allowed country
  is eligible whether or not it is in one of your named cities; being in one
  raises its `prefit_score`. Narrowing to specific cities is what `countries`
  plus your own reading of the row is for.
- Out of scope in v1: Workday, SuccessFactors, per-company custom boards,
  `recruitee`, HTML scraping of career pages for postings, and application
  submission.
