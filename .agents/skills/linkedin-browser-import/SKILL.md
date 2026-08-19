---
name: linkedin-browser-import
description: Import personalized LinkedIn job recommendations from the user's authenticated ChatGPT desktop browser into this repository's local job database. Use for LinkedIn recommended jobs or other signed-in LinkedIn Jobs pages; do not use for public LinkedIn search, which belongs to linkedin-search.
---

# LinkedIn Browser Import

Use the in-app Browser's existing LinkedIn session to collect jobs the user asks
to import. This skill reads job pages and updates the local board; it does not
apply, message people, follow companies, save jobs on LinkedIn, or change account
settings.

## Workflow

1. Work from this repository's root. Confirm
   `tools/import_linkedin_browser.py` exists before browsing.
2. Open the requested LinkedIn Jobs page in the authenticated in-app Browser. If
   LinkedIn requires login or a checkpoint, stop and ask the user to complete it;
   never request, copy, or store credentials or session data.
3. Collect the currently requested recommendations. Unless the user gives a
   different limit, stop after 30 unique postings or the end of the currently
   available results, whichever comes first.
4. Open each job detail and collect:
   - `title`, `company`, and `linkedin_url` from the LinkedIn job detail (required)
   - `url`: the final company application or careers URL when available; otherwise
     use the same value as `linkedin_url`
   - `location`, `posted`, `deadline`, and `description` when visible
   Prefer inspecting the external Apply link target. If necessary, follow it only
   far enough to capture the final company/ATS URL, then stop before signing in,
   completing a form, or submitting anything. Do not store a LinkedIn redirect or
   tracking wrapper when a final company URL is available. Do not invent missing
   values. Skip promoted cards or navigation links that are not jobs.
5. Deduplicate the collected batch by numeric LinkedIn job ID. Write a temporary
   UTF-8 JSON file in this shape:

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
         "description": ""
       }
     ]
   }
   ```

6. When the user requests a preview, run:

   ```bash
   python3 tools/import_linkedin_browser.py --dry-run <temporary-json-file>
   ```

   Otherwise import with:

   ```bash
   python3 tools/import_linkedin_browser.py <temporary-json-file>
   ```

   Never edit `job_scraper/seen_jobs.json`, `job_scraper/jobs.md`, or either CSV
   directly. The importer owns validation, locking, conservative cross-source
   deduplication, and persistence to the canonical JSON state, which the board
   reads directly. It prefers the final company URL, retains the LinkedIn URL as
   provenance, and records a recognizable ATS vendor automatically. Markdown and
   CSV are optional exports created only through `tools/jobs_md.py export-md` or
   `export-csv`.
7. Report the importer summary: received, added, already known, cross-source
   collapsed, possible duplicates, and gated. Also report any cards skipped
   before import and why.
