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

### Narrative contract

Follow the canonical base's TAILORING RULES and `03-writing-style.md` for the
complete narrative policy. Facts and scope are fixed; wording and evidence order
are editable throughout. COVER_NARRATIVE_V1 validates supported paragraph structure,
not factual truth. Independent content review remains mandatory. Older bases that
explicitly declare COVER_LIBRARY_V1 retain their exact-block contract.

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

### Legacy list templates only: itemize inside `\lettercontent{}`

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

\lettercontent{[TAILORED_MOTIVATION]} % specific attraction and personal reason, normally two or three sentences

\lettercontent{[RELEVANT BACKGROUND]}
\lettercontent{[LEADING EVIDENCE]}
\lettercontent{[COMPLEMENTARY EVIDENCE]}
\lettercontent{[CONTRIBUTION AND CLOSING]}

% Do not add a line break inside \closing{}; cover.cls appends its own \\, and a
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

### Length and narrative

At most 380 body words and exactly one page. Use separate `lettercontent`
paragraphs without bullets. Keep the base preamble, margins and typography intact.
Shorten repetition and less relevant detail across the narrative. Do not pad to a
word target or compress the motivation to accommodate every project. Read the whole
letter for distinct paragraph purposes and natural transitions.

### LaTeX Special Characters
- Underscore: `\_`
- Ampersand: `\&`

### Non-English Cover Letters
- Same template structure, just write content in the posting's language
- Adjust date format to local convention
- Adjust closing to local convention (e.g. "Med venlig hilsen," for Danish)

## Checklist Before Finalizing

- Every claim, metric, credential and implication matches source evidence.
- Personal, course, research and production work are accurately distinguished.
- Motivation is sincere and specific without explaining the employer's product.
- Evidence adds complementary strengths; background and closing support the narrative.
- No repeated manifesto, stock opening, keyword collage or unresolved placeholders.
- Contact details are verified and unknown address lines removed.
- Exactly one page, at most 380 body words, unchanged layout, visually inspected.

## Submission Guidelines (Best Practice)
- Submit only the documents the employer requests
- Export as PDF to preserve formatting
- Name files clearly: "[Your Name] CV" and "[Your Name] Cover Letter"
- Follow all employer instructions regarding anonymity or specific materials
