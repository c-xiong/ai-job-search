---
name: linkedin-browser-import
description: Import personalized LinkedIn job recommendations from the user's signed-in browser (Claude in Chrome, or an agent's in-app browser) into this repository's local job database. Use for LinkedIn recommended jobs or other signed-in LinkedIn Jobs pages; do not use for public LinkedIn search, which belongs to linkedin-search.
---

# LinkedIn Browser Import

Use the browser's existing LinkedIn session to collect jobs the user asks to
import. This skill reads job pages and updates the local board; it does not
apply, message people, follow companies, save jobs on LinkedIn, or change account
settings.

Detail pages are the whole cost of this skill. A results list is cheap to read;
every opened job is several thousand tokens. So the flow is two passes: read the
list, let the importer say which cards are new, and open only those.

## Token discipline

- Read pages as **text** (page-text / accessibility-tree tools). Take a
  screenshot only when a text read comes back empty or a control cannot be found
  any other way.
- Open a job at its direct URL, `https://www.linkedin.com/jobs/view/<id>/`,
  rather than clicking through the list's split pane: it loads just that job.
- Do not follow the external Apply link. Read its `href` (see step 5).
- Keep the user's limit. Default: at most **25 detail pages** per run.

## Workflow

1. Work from this repository's root. Confirm
   `tools/import_linkedin_browser.py` exists before browsing.
2. Open the requested LinkedIn Jobs page (default: "Top job picks for you",
   `https://www.linkedin.com/jobs/collections/recommended/`). If LinkedIn asks
   for login or a checkpoint, stop and ask the user to complete it; never
   request, copy, or store credentials or session data.
3. **List pass.** From the results list only, collect for each card `title`,
   `company`, `location`, and `linkedin_url` (the numeric job ID as
   `https://www.linkedin.com/jobs/view/<id>`). Scroll or page until about 60
   cards or the end of the results. Skip promoted cards and non-job links.
   Drop cards that fail the candidate's hard gates *on the card alone* - the
   deal-breakers and target locations in `CLAUDE.md` (for example a title in a
   language the candidate does not work in, or a location they would not move
   to that is not remote). Do not pre-judge anything subtler; the fit
   scorer does that with the full text.
4. Write the cards to `/tmp/linkedin_cards.json` as `{"jobs": [...]}` and run:

   ```bash
   python3 tools/import_linkedin_browser.py --check /tmp/linkedin_cards.json
   ```

   It writes nothing. Open only the cards under `new`, up to the limit. Skip
   `known` (the LinkedIn ID is on the board already). Skip `likely_known` too
   (same company and title already came from another source), but list them in
   the report so the user can ask for any that is really a different job.
5. **Detail pass.** For each card to open, collect:
   - **`description`: the full posting text.** Expand "see more" first and take
     the whole body, verbatim; never summarise, translate, or trim it. This is
     the reason for opening the page: a row without a body is capped at a
     `medium` fit band.
   - `url`: the final company or ATS application URL. Read the Apply button's
     `href`. If it is a LinkedIn redirect carrying the target in a `url=` query
     parameter, URL-decode that. For Easy Apply, or when no external target is
     readable, use the `linkedin_url`. Never store a LinkedIn redirect or
     tracking wrapper.
   - `posted` (as `YYYY-MM-DD` when an absolute date is derivable, else as
     shown) and `deadline`, when visible. Do not invent missing values.
   - If the posting body turns out to be written in German, or states German as
     a requirement, still include it; the importer's German gate records it.
6. Write `/tmp/linkedin_jobs.json` in this shape:

   ```json
   {
     "jobs": [
       {
         "title": "Machine Learning Engineer",
         "company": "Example",
         "location": "Zurich, Switzerland",
         "url": "https://job-boards.greenhouse.io/example/jobs/1234567",
         "linkedin_url": "https://www.linkedin.com/jobs/view/1234567890",
         "posted": "2026-08-19",
         "deadline": null,
         "description": "About the role\nYou will build retrieval pipelines in Python...\n\nRequirements\n- 2+ years with PyTorch\n..."
       }
     ]
   }
   ```

   Then import (add `--dry-run` first when the user asks for a preview):

   ```bash
   python3 tools/import_linkedin_browser.py /tmp/linkedin_jobs.json
   ```

   Never edit `job_scraper/seen_jobs.json`, `job_scraper/jobs.md`, or either CSV
   directly. The importer owns validation, locking, cross-source deduplication,
   the German gate, and persistence to the canonical JSON state, which the board
   reads directly. It prefers the final company URL, keeps the LinkedIn URL as
   provenance, and records a recognizable ATS vendor automatically.
7. Report briefly: cards listed, dropped by the card-level gates, `known`,
   `likely_known` (title and company), opened, and the importer summary
   (added, already known, collapsed, possible duplicates, gated). Do not echo
   posting text back.

## Without a shell

If the browser agent cannot run commands in this repository (for example a
Claude app chat with Chrome but no repo access), stop after step 3, give the
user the cards JSON, and wait for them to paste back the `--check` output
before opening any detail page. At the end, hand over `linkedin_jobs.json`
for the user to import.
