---
framework_version: 2.0.0
---

# CV Layout and Tailoring Rules

<!-- SETUP: /setup copies this file to 05-cv-templates.md. Fill in the base table and
the master's location for your own CV; the pipeline rules below apply unchanged. -->

Read by the **draft** stage (and by the repair stage for page fitting). Facts
come from the evidence sources named in `/apply`'s shared rules, never from this
file; this file says how the tailored CV is laid out and edited.

## The master and the tailored copy

- **Master:** `cv/my_cv.tex` - your own CV source. It may be a plain file or a
  symlink to a CV maintained in another repository. The pipeline **reads it and
  never writes it**: its guard refuses every write that resolves to the master,
  through any alias, and `/setup` never creates or edits it. If it is missing,
  a run stops and says so.
- **Tailored copy:** each application gets its own independent file. The
  supervisor seeds `documents/runs/<run>/work/cv.tex` with a byte copy of the
  master plus pinned variant switches at the top:

  ```latex
  \newcommand{\cvrole}{ai}      % the selected base
  \newcommand{\cvcountry}{ch}   % ch by default, or de
  ```

  Declare the switches in your master with `\providecommand`, so the pins win
  and the copy never reads the master again. The draft stage tailors that file
  in place; after its checks pass the supervisor publishes it to
  `cv/main_<company>_<role>.tex` and the PDF to `cv/build/`. A tailored file
  never `\input`s the master.

## Content bases

<!-- SETUP: describe your bases. A master without switches is fine: every run
then uses the same base and the table below has one row. -->

| Base | Use for | Emphasis |
|---|---|---|
| `sde` | Software, backend, full-stack and platform roles | [YOUR_SDE_EMPHASIS] |
| `ai` | AI/ML/LLM/NLP and data-science roles | [YOUR_AI_EMPHASIS] |

`auto` resolves to `ai` for AI/LLM/NLP/ML/Data Scientist titles and `sde`
otherwise. The country switch selects a work-authorisation line; it is a legal
statement you choose, so it is **never inferred from the posting's location**.

## Layout and toolchain

The stock pipeline assumes a single-column LaTeX `article` CV compiled with
**pdfLaTeX** from `cv/` to **exactly one page** (`tools/board/templates.py` is
the single source for engine, directory and page count). Use
`\input{glyphtounicode}` and `\pdfgentounicode=1` so the text layer extracts
cleanly. A different layout or toolchain is registered with `/add-template`,
whose `Page limit` then overrides the stock one.

`tests/fixtures/latex/cv_fixture.tex` is an anonymous example of this layout,
used by CI. It is never a source of candidate facts and never a fallback.

## Tailoring one page

A one-page CV is a selection, not a summary. Targeting may reorder, trim and
re-emphasise; it may not add facts. Keep every number exactly as sourced.

### Keyword admission: specific and defensible, not exhaustive

Generic graduate-programme language (analytical ability, curiosity, teamwork,
broad cloud or programming exposure) guides evidence selection but is not a
keyword to add. For a **specific named hard skill** an ATS may match literally:

1. **Documented** - a source names it or proves direct use: add the exact term,
   preferably beside the experience that demonstrates it.
2. **Credible adjacent / interview-ready** - the exact term is not recorded,
   but close concrete evidence makes genuine familiarity a reasonable inference
   and it can be refreshed before interview: add it **only** on the skills
   line, never in an experience or project bullet, never with a claim of
   production use, duration or proficiency. Record it in the brief
   (`status: "adjacent"`) so it becomes an interview-preparation item.
3. **Unsupported** - learning from zero, or a certification, language,
   clearance, years of experience or domain credential: leave it out.

### Fitting exactly one page

Cut by signal, not by section. Score each candidate line on (1) relevance to
this posting, (2) uniqueness within the CV, (3) whether the cover letter leans
on it; cut the lowest total first. Near misses: `\enlargethispage{\baselineskip}`
or `\needspace{4\baselineskip}` before an entry whose heading would separate
from its bullets. Never shrink fonts, margins or line spacing to force a fit.

## ATS text layer

The supervisor extracts the PDF text layer with `pdftotext -layout` and checks:
text present with no `(cid:..)`/replacement characters; email and phone as
literal text; sane reading order; and **CV keyword coverage measured on the
CV's own text**. Write date ranges with an ASCII hyphen (`2024-2026`); some
importers drop an en-dash (`--`) range.
