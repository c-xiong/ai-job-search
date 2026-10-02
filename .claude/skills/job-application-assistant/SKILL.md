---
name: job-application-assistant
description: >
  Assists with job applications: evaluating job postings, tailoring CVs, writing cover letters,
  and preparing for interviews. Triggers on keywords like: job posting, job application, CV,
  cover letter, resume, interview prep, job fit, career, application, apply, ansøgning, stilling
allowed-tools: Read, Glob, Grep, WebFetch, WebSearch, Bash, Edit, Write, AskUserQuestion
framework_version: 1.4.0
---

# Job Application Assistant

---

## Workflow

When the user provides a job posting (URL or text) and wants documents, follow the
staged workflow in `.claude/commands/apply.md` (its "Shared rules" are the evidence,
trust and skill-admission rules for every step below). There is no fit score or
approval pause on this path; a fit evaluation runs only when the user asks for one.

### Step 1: Save the Posting and Map Requirements
- Fetch the job posting content (use WebFetch for URLs). **A 403 is not a dead end** - follow the escalation order in `09-web-research.md` before concluding a page is unavailable, and prefer the employer's own careers posting over an aggregator listing
- Keep the **full posting text verbatim** for Step 3b to archive - never a summary
- Map the decisive requirements to real evidence (the brief in `/apply` "Stage: draft"), and surface any explicit hard conflict with the deal-breakers in CLAUDE.md before drafting
- Only on request: score the posting with `04-job-evaluation.md`, or research the company beyond the specific statements a cover letter needs

### Step 2: Tailor CV
- Read `cv/my_cv.tex`, using the selected `sde` or `ai` content base as the starting point; existing tailored CVs are phrasing references only
- Follow the guidelines in `05-cv-templates.md`
- Create `cv/main_<company>_<role>.tex` with tailored content
- Adjust: profile statement, skills section, experience bullet emphasis, section order
- Apply `/apply`'s Skill Admission Gate: add exact, role-specific hard-skill keywords when documented or credibly adjacent and interview-ready; keep inferred terms at skills level only, never inventing project use. Ignore broad graduate-programme language as keyword targets

### Step 3: Write Cover Letter
- Follow the writing style rules in `03-writing-style.md` (critical: no em-dashes, no cliches)
- Follow the template structure in `06-cover-letter-templates.md`
- Start from the one cover base, `cover_letters/my_cover.tex`, for every role: obey its TAILORING RULES block, write a tailored motivation paragraph, select one exact fixed introduction and 2-3 distinct COVER_LIBRARY_V1 highlights by the posting's tasks, retain USE_COVER_TEXT markers, then write a tailored closing with a different purpose
- Keep fixed wording unchanged in draft, review and repair; never combine nlp_research with nlp_models or research_interface with publication. Use named methods only where the approved text explains relevant behavior or engineering decisions
- Follow the base's motivation/closing and evidence-selection rules, including considering nlp_models for applied AI work beyond research when it adds a distinct strength. Budget both tailored paragraphs before optional evidence. At most 380 body words, usually 250–360 without padding; exactly one page with the base's approved margins and typography. Preserve its preamble exactly apart from whitespace/comments; add no packages or formatting overrides
- Create `cover_letters/cover_<company>_<role>.tex`
- Ensure the letter connects the strongest specific experience to the role rather than answering every requirement; use no more than one honest adjacent-skill bridge and omit generic programme language

### Step 3b: Record the Application
- Run this once both documents exist. A CV or cover letter drafted alone is not yet an application.
- Follow **`/apply` Step 6b** (`.claude/commands/apply.md`) exactly: same header, same match-then-update rule, same `drafted` row, same posting archive, same prohibition on touching `job_scraper/seen_jobs.json`. It is stated there once so the two paths cannot drift. Four of its values are named in `/apply`'s own terms: `cv_file`/`cover_letter_file` are the paths written in Steps 2 and 3 here, `source` is the posting URL from Step 1, `deadline` is the application deadline from the posting text Step 1 keeps verbatim (empty when the posting states none - never guess one), and the posting text item 7 archives is the one Step 1 read.
- This step exists here because `/scrape` Step 5 routes straight into this skill. Without it, that path writes two documents and records nothing.

### Step 4: Interview Preparation
- Follow the framework in `07-interview-prep.md`
- Prepare STAR-format answers for likely questions
- Identify role-specific talking points
- Draft questions the candidate should ask the interviewer

---

## Reference Files

| File | Purpose |
|------|---------|
| `01-candidate-profile.md` | Education, experience, skills, publications, awards |
| `02-behavioral-profile.md` | Behavioral assessment, strengths, ideal environments |
| `03-writing-style.md` | Tone, structure, do's and don'ts |
| `04-job-evaluation.md` | Scoring framework for job fit |
| `05-cv-templates.md` | LaTeX CV structure and tailoring rules |
| `06-cover-letter-templates.md` | LaTeX cover letter structure and tailoring rules |
| `07-interview-prep.md` | STAR examples, tough questions, roleplay guidelines |
| `08-application-forms.md` | Portal free-text fields: self-introduction, project entries, character-limited pitches |
| `09-web-research.md` | Fetching postings and company pages: trust boundary, the WebFetch 403 fallback, escalation order, claim verification |

---

## Quick Commands

The user may also ask for individual steps without the full workflow:
- "Evaluate this job posting" - a fit evaluation with `04-job-evaluation.md` (on request only)
- "Write a CV for [company]" - Step 2 only
- "Write a cover letter for [role] at [company]" - Step 3 only
- "Help me prepare for an interview at [company]" - Step 4 only
- "What jobs should I look for?" - Career strategy discussion using profile + evaluation framework
