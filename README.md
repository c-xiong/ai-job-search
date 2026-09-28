<p align="center">
  <img src="assets/mascot/pip_flight_loop.gif" alt="Pip, the courier bird (from the upstream project)" width="160">
</p>

# AI Job Search: Switzerland & Germany edition, with a local job board

[![CI](https://github.com/c-xiong/ai-job-search/actions/workflows/ci.yml/badge.svg)](https://github.com/c-xiong/ai-job-search/actions/workflows/ci.yml)

A local, [Claude Code](https://claude.com/claude-code)-driven job search for the
**DACH market** (Switzerland, Germany, Austria). It collects postings from LinkedIn,
freehire and your target companies' own ATS boards, puts them in a **browser job board**
on `127.0.0.1`, and turns the ones you pick into a one-page CV and a one-page cover
letter compiled with LaTeX. It also tracks where each application stands.

> **This is a fork.** The framework comes from
> **[MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search)**,
> built by Mads Lorentzen to run his own job search in Denmark. The profile onboarding,
> `/apply`, `/interview`, `/outcome`, `/rank`, `/upskill` and the portal-skill model all
> come from there. If this saves you time, please star and
> [support the original](https://ko-fi.com/madslorentzen). What this fork adds is listed
> [below](#what-this-fork-adds).
>
> This is an independent project, not affiliated with or endorsed by Anthropic.

## What this fork adds

| Area | Upstream | This fork |
|---|---|---|
| **Market** | Danish job portals (Jobindex, Jobnet, …) | DACH: LinkedIn, freehire and company ATS boards. The Danish portals are removed |
| **Where you work** | Terminal only | A **local job board** in the browser (Board / Companies / Applications) that drives the whole loop. The slash commands still work |
| **Company watching** | – | `ats-search` reads Greenhouse, Ashby, Lever, Personio, Workday and SmartRecruiters boards for the companies you list. The board retries ATS detection on every fetch |
| **Adding jobs by hand** | Paste into `/apply` | **Add job** dialog and a `+ JobFlow` bookmarklet. You can also import LinkedIn's signed-in "recommended for you" list through the browser |
| **Triage** | `/rank` (LLM) | A deterministic **fit score** computed without an LLM, so the list has a useful order before you spend any tokens. `/rank` still overrides it |
| **Language gate** | Language table | A **German-requirement screen**: postings that state German as a job condition are filed as `gate` (with the matching sentence quoted), not silently dropped |
| **`/apply`** | Drafter → reviewer → revise | A **staged, checkpointed pipeline** you can start from the board, behind a write allowlist and per-run budgets. Interrupted runs can **Continue** |
| **Documents** | Tailored CV per job | The CV master is **read-only**. Each application uses its `sde` or `ai` variant unchanged, and the cover letter is tailored from a per-role base (Swiss/German A4 layout, XCharter) |
| **Application status** | CSV tracker | A **Notion database** is the source of truth for application stage. The CSV is a local cache |
| **Privacy** | Private fork expected (`/setup` writes into tracked files) | **Public-safe**: every personalised file is gitignored, only `*.example` twins are tracked, and CI fails if a personal file is ever staged |

## How it fits together

```
 Collect                     Triage                    Apply                        Track
 ─────────────────────────   ───────────────────────   ──────────────────────────   ─────────────────
 LinkedIn (public search)    Board: filter, sort by    Board → Draft → /apply       Applications tab:
 LinkedIn (signed-in, via    fit score, mark           pipeline: posting → write    Review → Send
   browser import)           yes / no / applied        & check → build & verify     (portal link, notes)
 freehire (DE/CH/AT)         German-gate screen        pdfLaTeX CV (1 page)         Notion "Stage"
 Company ATS boards          /rank for a deeper,       XeLaTeX letter (1 page)      /outcome, /gmail-sync,
 Add job / bookmarklet       LLM-scored shortlist      Continue / Regenerate        /interview
```

## Quick start

### 1. Prerequisites

- [Claude Code](https://claude.com/claude-code) CLI (the board starts `claude` for `/apply` runs)
- Python 3.10+ (the board and tools use only the standard library)
- [Bun](https://bun.sh) for the portal CLIs
- A TeX distribution with `pdflatex` and `xelatex` (MacTeX / TeX Live; minimal installs: see [SETUP.md](SETUP.md#minimal-tex-install-tinytexbasictex))
- Optional: `pdftotext` (poppler) for the ATS text-layer check, and a Notion integration token for status sync

### 2. Clone and create your private working copies

```bash
git clone https://github.com/c-xiong/ai-job-search.git
cd ai-job-search

# Personal files are gitignored. Copy each tracked template to its real name
# (-n never overwrites a file you already have):
cp -n CLAUDE.example.md CLAUDE.md
for f in .claude/skills/*/*.example.md; do cp -n "$f" "${f%.example.md}.md"; done
for f in job_scraper/*.example.json; do cp -n "$f" "${f%.example.json}.json"; done
```

If you want your filled-in profile in a remote, push it to a **private** repository and
keep this one as `upstream`. A GitHub fork of a public repo is always public.

### 3. Install the portal CLIs

```bash
for tool in ats-search linkedin-search freehire-search; do
  (cd .agents/skills/$tool/cli && bun install)
done
```

### 4. Build your profile and put your documents in place

```bash
claude
/setup          # reads documents/, a pasted CV, or interviews you
```

Then add the two sources the pipeline reads and never edits:

- `cv/my_cv.tex`: your one-page CV master (a file or a symlink). The shape is shown in
  `tests/fixtures/latex/cv_fixture.tex`. Optional `\cvrole{sde|ai}` switches give you role variants.
- `cover_letters/my_cover.tex`: your cover-letter base on `cover.cls`, optionally with
  `my_cover_sde.tex` / `my_cover_ai.tex` per role. The rules are in
  `.claude/skills/job-application-assistant/06-cover-letter-templates.md`.

List the companies you want to watch in `job_scraper/companies.json`, and edit
`fit_profile.json` / `company_affinity.json` to adjust the fit score.

### 5. Open the board

```bash
python3 tools/jobs_board.py          # http://127.0.0.1:8765/?t=<token>
```

Click **Fetch new jobs**, work through the list, move the good ones to **Draft**, and start
an application run. The [board guide](BOARD.md) walks through every view.

Prefer the terminal? `/scrape`, `/rank` and `/apply <url or pasted posting>` do the same work
from Claude Code.

## Commands

| Command | What it does |
|---|---|
| `/setup` | Build your profile from `documents/`, a pasted CV, or an interview. `/setup --section search` re-does search config only |
| `/scrape` | Run every enabled portal skill, dedupe, present matches |
| `/rank` | LLM-score new postings into a ranked shortlist with strengths and gaps |
| `/apply` | Posting → requirement brief → CV variant + tailored letter → review → compile → verify (1 page each) |
| `/outcome` | Record interview stages, offers and rejections; archive what was sent; `/outcome followup` drafts follow-ups |
| `/interview` | Stage-specific prep pack from the archived application, plus mock interview |
| `/notion-sync` | Set up and run the Notion ⇄ tracker sync |
| `/gmail-sync` | Propose status changes from your Gmail, for you to approve |
| `/upskill` | Skill-gap heatmap and learning plan across tracked postings |
| `/expand` | Enrich your profile from GitHub, Scholar, course syllabi you've linked |
| `/html-report` | Offline HTML dashboard of the tracker |
| `/add-portal` · `/add-template` | Generate a portal skill for another job board · register your own CV/letter template |
| `/reset` | Wipe profile data and/or `documents/` (asks you to type `RESET`) |

Useful scripts (all stdlib Python):

```bash
python3 tools/fetch_jobs.py --sources ats,linkedin,freehire --dry-run   # what the Fetch button runs
python3 tools/fit_score.py --explain <url>                              # why a row got its fit band
python3 tools/notion_sync.py check|pull|push                            # Notion sync outside the board
python3 tools/profile_drift.py                                          # profile claims missing from your CV
```

## Privacy: what never leaves your machine

This repository is public and contains no personal data. Everything personal is
gitignored:

- **Profile:** `CLAUDE.md`, the filled-in skill files (`01-`, `02-`, `04-`, `05-`, `07-`, `search-queries.md`)
- **Documents:** `cv/my_cv.tex` and every generated CV/letter, `cover_letters/my_cover*.tex`, `documents/**`
- **Search state:** `job_scraper/*.json` except the `*.example.json` twins, stored postings, ATS cache, run records and transcripts
- **Tracking:** `job_search_tracker.csv`, `gmail_sync/`, `reports/`, `upskill/*.md`
- **Secrets:** `.env*`, the board token, `notion_sync.json`, `.claude/settings.local.json`

`tools/security_guards.py` pins these ignore rules, and the CI `placeholder-integrity` job
fails if a personal path is staged or an `*.example` file loses its `[YOUR_*]` placeholders.
Run `python3 -m unittest discover -s tests` before pushing.

Posting text is treated as untrusted input. Board-driven runs write only to an explicit
allowlist, and the board binds to `127.0.0.1` behind a random token. See [SECURITY.md](SECURITY.md).

## Documentation

- [SETUP.md](SETUP.md): full install, TeX, Notion, and pulling upstream updates
- [BOARD.md](BOARD.md): the local job board, fetching, companies, applications, budgets
- [documents/README.md](documents/README.md): what to put in `documents/` for `/setup`
- [.agents/skills/ats-search/SKILL.md](.agents/skills/ats-search/SKILL.md): the company registry and supported ATS vendors
- [CHANGELOG.md](CHANGELOG.md): upstream releases plus this fork's `[Unreleased]` changes
- [CONTRIBUTING.md](CONTRIBUTING.md): what belongs in this fork and what belongs upstream

## Acknowledgements

- [Mads Lorentzen](https://github.com/MadsLorentzen) for the original
  [ai-job-search](https://github.com/MadsLorentzen/ai-job-search) framework, its mascot, and its methodology
- [Mikkel Krogsholm](https://github.com/mikkelkrogsholm) for the original portal CLI skills
- Built with [Claude Code](https://claude.com/claude-code)

## License

MIT, as upstream. See [LICENSE](LICENSE).
