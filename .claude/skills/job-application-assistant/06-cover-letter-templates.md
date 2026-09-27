---
framework_version: 1.4.0
---

# Cover Letter Templates and Tailoring Guide

The personalized content bases follow the same `sde`/`ai` switch that selects
the CV variant (ML and data-science roles use `ai`):

| Role base | Cover base |
|---|---|
| `sde` | `cover_letters/my_cover_sde.tex` |
| `ai` | `cover_letters/my_cover_ai.tex` |
| fallback (variant file absent) | `cover_letters/my_cover.tex` |

Each base opens with a **TAILORING RULES** comment block; those rules are
binding and take precedence over the generic guidance below. Existing
job-specific letters are phrasing references only.

### Base-content contract

- **Structure is fixed:** short `[WHY THEM]` opening -> one who-I-am paragraph -> 3-4 bold-labelled bullets -> optional one-sentence `[EXTRA]` -> closing (with `[RELOCATION]` only for roles outside the Zurich area). Target 230-280 words, one page.
- **Tailor every time:** recipient block, subject, salutation, `[WHY THEM]`, bullet choice and order, and the optional `[EXTRA]`/`[RELOCATION]` slots.
- **Bullets come from the base's active list and BULLET BANK only.** Bold labels may echo the posting's wording; facts and numbers never change, and a bullet is never stretched to cover a requirement it does not meet. Respect each base's DO NOT CLAIM list.
- **Do not silently regenerate:** changing the who-I-am paragraph or adding evidence outside the bank requires a stated reason in the final tailoring report.

## Template: Custom cover.cls (XeLaTeX)

Cover letters use a custom LaTeX document class (`cover.cls`) with XCharter for body copy and Lato/Raleway for the formal header and metadata. This mirrors the CV's Charter-led typography while keeping the letter hierarchy crisp.

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
cd cover_letters && xelatex -interaction=nonstopmode cover_<company>_<role>.tex
```

Expected output: `Output written on cover_<company>_<role>.pdf (1 page, ...)`. Any page count other than 1 is a failure that must be fixed before presenting to the user.

## Compile-and-Inspect Loop (MANDATORY)

After writing the cover letter and before presenting to the user, always compile and visually inspect the PDF. Iterate until the layout is clean:

1. Run `xelatex -interaction=nonstopmode cover_<company>_<role>.tex`
2. Confirm page count is exactly 1 and compile succeeded
3. Read the PDF via the Read tool and visually check: signature fits at the bottom, no text cut off, bullet font matches body

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
\begin{itemize}
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
\usepackage{fancyhdr}

\pagestyle{fancy}
\fancyhf{}

\rfoot{Page \thepage \hspace{0pt}}
\thispagestyle{empty}
\renewcommand{\headrulewidth}{0pt}
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

\lettercontent{[Opening paragraph - role, connection to background, 2-3 sentences]}

\lettercontent{[Body paragraph - most relevant experience, introducing the bullet list]}

{\raggedright\letterbodyfont
\begin{itemize}
    \item [Concrete achievement/skill 1]
    \item [Concrete achievement/skill 2]
    \item [Concrete achievement/skill 3]
\end{itemize}\par}

\lettercontent{[Connection to company - why this role, why this company specifically]}

\lettercontent{[Personal fit paragraph - behavioral strengths, team contribution, 2-3 sentences]}

\lettercontent{I look forward to hearing from you.}

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

- Lead with the two or three strongest documented matches and show what the candidate can do for this role.
- Ignore generic graduate-programme language as copy targets. Demonstrate a useful trait through evidence instead of repeating words such as curious, analytical, collaborative, adaptable, or eager to learn.
- A specific hard skill inferred from close adjacent evidence may be mentioned at most once and only at the honest level of `familiarity` or `working knowledge`. Never imply that it was used in a named project or production system unless a factual source says so.
- Do not confess every missing nice-to-have. Bridge an unsupported gap only when it is decisive to the role and the adjacent experience makes a credible case.
- Eligibility facts such as language, certification, clearance, degree status, availability, and work authorization are never inferred.

### Salutation
- If you know the hiring manager's name: "Dear [First Last],"
- If you know the team: "Dear [Company] hiring team,"
- Generic: "Dear [Company]," (avoid "To whom it may concern")

### Length - Hard 1-Page Limit
- Target: 1 page including signature block
- Maximum: **never exceed 1 page**
- **Word budget: 250-300 words** of body text (not counting LaTeX markup). This is the safe maximum. 350 words will overflow.
- **Always count**: opening paragraph + bullet list paragraph + closing paragraph = 3 blocks. Add a 4th only if the others are short.
- When adding company-specific content, trim other content to compensate rather than adding net length

### Line Spacing
- Add `\usepackage{setspace}` and `\setstretch{1.0}` if the letter is long and needs to fit on one page
- Use `\vspace{.5cm}` between major sections for readability (only if space permits)

### Bullet Lists
- Place `\begin{itemize}...\end{itemize}` **outside** a `\lettercontent{}` block (see "Known template pitfall" above), wrapped in `\letterbodyfont` so the bullets use the same XCharter family and true bold face as the body
- 3-5 bullets is ideal
- Start each bullet with bold label or action verb
- Use `\textbf{Label:}` for category-style bullets

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
- [ ] Body uses XCharter; sans fonts are limited to header/metadata; bold labels render with a true bold face
- [ ] Verified employer/contact address is left aligned; unresolved optional lines are removed, with no visible placeholders
- [ ] Place/date is right aligned; the exact role and any reference number appear in a bold, left-aligned subject line
- [ ] No em-dashes (use commas or periods instead)
- [ ] No cliches or empty filler
- [ ] Every claim backed by specific example
- [ ] No generic programme-language keyword stuffing or requirement-by-requirement narration
- [ ] Any adjacent inferred skill is levelled honestly and does not imply invented usage
- [ ] Forward-looking framing: focuses on tasks you'll solve, not just past duties
- [ ] Motivation section references this specific company's mission/values
- [ ] Company name and role are correct throughout
- [ ] Date is current
- [ ] Fits on one page
- [ ] Language matches the job posting language
- [ ] Salutation is appropriate (named person if possible)
- [ ] Headline is engaging and specific, not generic

## Submission Guidelines (Best Practice)
- Submit only the documents the employer requests
- Export as PDF to preserve formatting
- Name files clearly: "[Your Name] CV" and "[Your Name] Cover Letter"
- Follow all employer instructions regarding anonymity or specific materials
