---
framework_version: 1.7.0
---

# Cover Letter Templates and Tailoring Guide

One personalized base serves every role: `cover_letters/my_cover.tex`. The
CV keeps its `sde`/`ai` variants, but the letter does not follow them - AI, ML,
software and research letters share one structure, and what differs is which
evidence leads, chosen from the posting's tasks.

The base opens with a **TAILORING RULES** comment block, followed by an
**EVIDENCE BANK** and a **DO NOT CLAIM** list; all three are binding and take
precedence over the generic guidance below. Existing job-specific letters are
phrasing references only, never templates for the next letter.

### Base-content contract

- **Assembly, not regeneration:** the canonical COVER_LIBRARY_V1 in the base
  contains exact prose. Write a tailored motivation, select one fixed introduction
  and two distinct highlights, then write a tailored closing. Add a third highlight
  or optional publication only when it adds relevant evidence and both tailored
  parts fit the budget. Choose by the posting's tasks, never a fixed narrative or
  the CV variant.
- **Fixed text:** copy selected COVER_TEXT entries verbatim into matching
  USE_COVER_TEXT markers. Only whitespace may change. Do not modify identity,
  numbers, terminology or final sentences. Never use nlp_research with nlp_models
  (two views of the same current research), or research_interface with publication
  (the same project). New variants belong in a separate owner-requested library edit;
  an explicitly requested pipeline/template update may change the base, while
  routine applications keep it read-only.
- **Tailored motivation:** one concise sentence before the fixed introduction
  explains what specific role/company work attracts the candidate. A second
  sentence is allowed only when it adds meaning. Use an owner-confirmed interest
  from `01-candidate-profile.md` and supported responsibilities or independently
  verified company facts. Do not invent passion or copy an agent motivation into
  unrelated roles. Keep this prose outside the fixed introduction's markers.
- **Introduction voice and identity:** each whole variant starts with what the candidate
  builds and combines the current role, research, completed MSc requirements and
  an engineering interest. Never add a second background paragraph or rewrite the
  fixed introduction for an employer. Do not invent a degree conferral date.
- **Tailored closing:** develop the opening's attraction through the candidate's
  confirmed career direction or values and one grounded contribution; one to three
  new sentences, combined when natural. The profile grounds personal motivations;
  the base supplies generation and evidence rules. Do not repeat the opening,
  retell projects, add generic praise or force a career-goal sentence. Reserve
  space for meaningful content before optional evidence, with no padding. Follow with the
  conditional relocation and exact invitation from the base. Recipient, role,
  date and verified location are editable metadata.
- **Scope and checks:** the CV/profile ground facts; the evidence bank supplies
  review context, not fresh prose. Keep personal/research/course/production work
  distinct. Preserve the fixed-library base's preamble exactly, including its
  explicit geometry and typography; only whitespace and comments may differ.
  Do not add packages or redefine fonts, margins or spacing. `cover_fixed_blocks`
  checks selected prose and the unchanged preamble.
- **One page, at most 380 body words:** usually 250–360, with no minimum and no
  padding. Remove verbose customised prose without erasing either tailored part.
  Omit optional publication or the least relevant third highlight before cutting
  needed motivation or contribution. Never rephrase fixed blocks to fit.

### Evidence selection

Use the posting's actual tasks to choose the fixed introduction and leading highlight;
the approved samples establish wording and layout, not a default evidence
combination or company closing for other employers. Select complementary work
for the next highlight:

- `contract_agent`: agent orchestration, retrieval, tool execution, citation
  checks, failure handling, tracing and regression evaluation.
- `jobjuniors`: translating product requirements into architecture and carrying
  implementation through testing and deployment.
- `jobjuniors_software`: expanded software-delivery evidence connecting product
  ownership with system design, failure handling and testing. Prefer this for
  relevant software roles; use the shorter `jobjuniors` for supporting delivery
  evidence in other roles. Never select both variants of the same internship.
- `nlp_research`: NLP pipelines, data preparation and research evaluation.
- `nlp_models`: hands-on fine-tuning, empirical model comparison, and quality
  versus memory/response-time judgement. Useful for applied AI, LLM product and
  agent engineering as well as research; an explicit fine-tuning requirement is
  not necessary when this adds relevant evidence of model-level judgement.
- `research_interface`: human–LLM research and its experimental interface;
  not a separate highlight for software delivery roles.
- `event_platform`: brief course-based Java/Spring Boot evidence when backend
  correctness or language/framework experience adds a relevant strength.
- `publication`: optional short natural paragraph after the list, explaining
  the interface work and naming the publication venue without authorship rank.
  Include only when relevant and within budget, never as a publication bullet.

Choose the two strongest complementary examples. `nlp_models` can complement
`contract_agent`: classification/model comparison is different evidence from
agent controls, tracing and regression checks. `jobjuniors` adds product delivery.
Use three only when each adds a relevant task or strength and both tailored parts
fit. Prefer `nlp_research` when pipelines, data or time-aware evaluation matter
more; omit model evidence for general software work if stronger examples match.
Do not infer production inference optimization, general superiority of smaller
models, or foundation-model training from the classification comparison.

Choose only one of `nlp_research` and `nlp_models`, and only one of
`research_interface` and `publication`. Preserve distinctions between independent
projects, research, course work and production work. The base remains the source
of the exact wording; these cues do not authorize new claims or rewrites.

Named methods are useful when they explain a system's behavior or a relevant
engineering decision. The approved agent paragraph connects LangGraph, hybrid
retrieval, citation checks and Langfuse to what the system does and how failures
are inspected. Keep that supported detail; avoid CV-style library inventories
that do not explain the work.

## Template: Custom cover.cls (XeLaTeX)

Cover letters use a custom LaTeX document class (`cover.cls`) with XCharter for body copy and Lato/Raleway for the formal header and metadata. This mirrors the CV's Charter-led typography while keeping the letter hierarchy crisp.

The approved default uses 23 mm left/right margins, 16 mm top, 20 mm bottom and
10.7 pt body type with 14 pt leading. Keep these settings and the base's spacing;
do not tighten them to fit longer prose. Exactly one page includes the signature.

### Swiss/German formal-letter layout

For a PDF attachment, use an **A4 formal business-letter frame** rather than a centred personal-brand header:

1. Candidate name and contact details at the top right, including email, phone, Website, LinkedIn and GitHub where available
2. Employer/contact postal block on the left
3. Place and current date on the right
4. Bold application subject naming the exact role and any reference number, without the label `Subject`
5. Personal salutation, body, closing, signature space and typed full name, all left aligned

This is the default for Swiss and German applications. If the application is pasted into a portal text field rather than uploaded as a PDF, omit the letter frame and provide body text only. Never invent a street address for either party. The reusable base may retain bracketed address slots; a final application must fill them from a verified source or remove unresolved optional lines, never ship visible placeholders.

**Output file:** `cover_letters/cover_<company>_<role>.tex`
**Compile with:** XeLaTeX (cover.cls requires fontspec)
**Font directory:** `cover_letters/OpenFonts/fonts/`

### Compile command

```bash
cd cover_letters && xelatex -output-directory=build -interaction=nonstopmode cover_<company>_<role>.tex
```

Expected output: `Output written on cover_<company>_<role>.pdf (1 page, ...)`. Any page count other than 1 is a failure that must be fixed before presenting to the user.

## Compile-and-Inspect Loop (MANDATORY)

After writing the cover letter and before presenting to the user, always compile and visually inspect the PDF. Iterate until the layout is clean:

1. Run `xelatex -output-directory=build -interaction=nonstopmode cover_<company>_<role>.tex`
2. Confirm page count is exactly 1 and compile succeeded
3. Read the PDF via the Read tool and visually check: signature fits at the bottom, no text cut off, list items use the body font

### Known template pitfall: itemize inside `\lettercontent{}`

The `\lettercontent{}` macro appends `\\` to its argument. This breaks when the argument ends in `\end{itemize}` because `\\` has no line to break after the environment closes, producing `! LaTeX Error: There's no line here to end.` and no PDF output.

**Wrong (breaks compile):**
```latex
\lettercontent{Here is how my experience maps:
\begin{itemize}
    \item ...
\end{itemize}}
```

**Correct — close `\lettercontent{}` before the list and wrap the list in `\letterbodyfont` so typography stays consistent:**
```latex
\lettercontent{Here is how my experience maps:}

{\raggedright\letterbodyfont
\begin{itemize}[leftmargin=1.2em, topsep=2pt, itemsep=3pt, parsep=0pt]
    \item ...
\end{itemize}\par}
\vspace{6pt}

\lettercontent{[next paragraph]}
```

The `\letterbodyfont` wrapper is mandatory: outside `\lettercontent{}`, a list otherwise loses the controlled XCharter size and leading even if it inherits the same base family.

## Document Structure

```latex
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% Cover Letter - [Company], [Role]
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

\documentclass[a4paper]{cover}
\usepackage{enumitem}
\geometry{left=2.3cm,right=2.3cm,top=1.6cm,bottom=2.0cm}
\pagestyle{empty}
\begin{document}

\senderblock{[YOUR_NAME]}{
  [CITY, COUNTRY]\\
  \href{mailto:[YOUR_EMAIL]}{[YOUR_EMAIL]} \enspace|\enspace [YOUR_PHONE]\\
  \href{[YOUR_WEBSITE_URL]}{[YOUR_LITERAL_DOMAIN]} \enspace|\enspace
  \href{[YOUR_LINKEDIN_URL]}{LinkedIn} \enspace|\enspace
  \href{[YOUR_GITHUB_URL]}{GitHub}
}

\recipientblock{[COMPANY]\\
{[CONTACT PERSON OR HIRING TEAM]}\\
{[DEPARTMENT, IF KNOWN]}\\
{[STREET AND NUMBER]}\\
{[POSTCODE, CITY, COUNTRY]}}

\currentdate{[CITY], \today}
\subjectline{Application for [ROLE] [REFERENCE NUMBER, IF APPLICABLE]}
\lettercontent{Dear [Name/Team],}

\lettercontent{[TAILORED_MOTIVATION]} % specific attraction, normally one sentence

% USE_COVER_TEXT [OPENING_ID]
\lettercontent{[OPENING_TEXT]}
% END_USE_COVER_TEXT

{\raggedright\letterbodyfont
\begin{itemize}[leftmargin=1.2em, topsep=2pt, itemsep=3pt, parsep=0pt]
% USE_COVER_TEXT [HIGHLIGHT_1_ID]
    \item [HIGHLIGHT_1_TEXT]
% END_USE_COVER_TEXT
% USE_COVER_TEXT [HIGHLIGHT_2_ID]
    \item [HIGHLIGHT_2_TEXT]
% END_USE_COVER_TEXT
\end{itemize}\par}
\vspace{6pt}

\lettercontent{[CLOSING]}    % develop motivation, grounded contribution, relocation if needed, invitation

% No trailing \\ inside \closing{} - cover.cls appends its own \\, and a
% doubled break triggers "! LaTeX Error: There's no line here to end."
\closing{Kind regards,}

\signature{[YOUR_NAME]}
\end{document}
```

## Key Commands Reference

| Command | Purpose |
|---------|---------|
| `\namesection{}{Name}{contact info}` | Header with name and contact |
| `\currentdate{date}` | Date field (use `\today` or explicit date) |
| `\lettercontent{text}` | Body paragraph (adds spacing after) |
| `\closing{text}` | Closing line |
| `\signature{name}` | Printed name below signature |

## Tailoring Guidelines

### Requirement selectivity

The cover letter is a short argument, not a requirement checklist or ATS keyword dump.

- Lead with the strongest documented match for the role's main problem; 2-3 highlights, each a different strength.
- Ignore generic graduate-programme language as copy targets. Demonstrate a useful trait through evidence instead of repeating words such as curious, analytical, collaborative, adaptable, or eager to learn.
- A specific hard skill inferred from close adjacent evidence may be mentioned at most once and only at the honest level of `familiarity` or `working knowledge`. Never imply that it was used in a named project or production system unless a factual source says so.
- Do not confess every missing nice-to-have. Bridge an unsupported gap only when it is decisive to the role and the adjacent experience makes a credible case.
- Eligibility facts such as language, certification, clearance, degree status, availability, and work authorization are never inferred.

### Salutation
- If you know the hiring manager's name: "Dear [First Last],"
- Otherwise: "Dear [Company] Team," (avoid "To whom it may concern")

### Length - Hard 1-Page Limit
- Target: 1 page including signature block
- Maximum: **never exceed 1 page**
- **Word budget:** usually 250–360 words of body text, including tailored motivation,
  fixed introduction, highlights and closing but excluding the letter frame and
  LaTeX markup. **380 is the hard
  ceiling.** There is no minimum; never pad toward the range.
- Tailored motivation, fixed introduction, two highlights and tailored closing is
  the default. A third highlight needs a distinct role contribution and room for
  both tailored parts; do not add background paragraphs or items to fill space.
- Budget the motivation and a meaningful closing before optional third evidence or
  publication. Trim verbose customised wording, then drop the least relevant
  optional block before reducing needed specificity. Never change spacing,
  stretch, fonts or fixed text to fit.

### Highlights
- Use the exact selected library paragraph, inside its USE_COVER_TEXT markers.
- Put itemize outside lettercontent and inside raggedright/letterbodyfont.
- Do not add labels or explanatory text outside the selected fixed blocks.

### LaTeX Special Characters
- Underscore: `\_`
- Ampersand: `\&`

### Non-English Cover Letters
- Same template structure, just write content in the posting's language
- Adjust date format to local convention
- Adjust closing to local convention (e.g. "Med venlig hilsen," for Danish)

## Checklist Before Finalizing
- [ ] PDF uses A4 and the formal Swiss/German letter frame
- [ ] Candidate contact block is top right and includes working email, phone, literal website domain, LinkedIn and GitHub links
- [ ] Body uses XCharter; sans fonts are limited to header/metadata; list items use the body font
- [ ] Verified employer/contact address is left aligned; unresolved optional lines are removed, with no visible placeholders
- [ ] Place/date is right aligned; the exact role and any reference number appear in a bold, left-aligned subject line
- [ ] No em-dashes (use commas or periods instead)
- [ ] No cliches or empty filler
- [ ] Every claim backed by specific example
- [ ] No generic programme-language keyword stuffing or requirement-by-requirement narration
- [ ] Any adjacent inferred skill is levelled honestly and does not imply invented usage
- [ ] A concise tailored motivation precedes the unchanged fixed introduction and names specific work connected to a confirmed interest
- [ ] The fixed introduction and leading highlight answer the posting's main problem; each highlight shows a different strength
- [ ] Named technical methods explain supported behavior or decisions; no CV-level inventories or unexplained metrics
- [ ] The closing develops the opening through confirmed career direction/values and an evidenced contribution, without repetition, a project recap or an "ideal fit" claim
- [ ] Company name and role are correct throughout
- [ ] Date is current
- [ ] Exactly one page, at most 380 body words, with the approved margins and typography
- [ ] Language matches the job posting language
- [ ] Salutation is appropriate (named person if possible)

## Submission Guidelines (Best Practice)
- Submit only the documents the employer requests
- Export as PDF to preserve formatting
- Name files clearly: "[Your Name] CV" and "[Your Name] Cover Letter"
- Follow all employer instructions regarding anonymity or specific materials
