# Contributing

This repository is a **public fork** of
[MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search), adapted for
job searching in Switzerland and Germany and extended with a local job board. Before you
open an issue or PR, decide where it belongs:

| Your change | Where it goes |
|---|---|
| General framework methodology (`/setup`, `/apply` rules, evaluation rubric, interview prep) that is not DACH- or board-specific | **Upstream**, following its [CONTRIBUTING.md](https://github.com/MadsLorentzen/ai-job-search/blob/master/CONTRIBUTING.md). This fork merges upstream releases |
| The board (`tools/board/`), `ats-search`, the fit score, the German screen, the Notion sync, DACH portal skills | **Here** |
| Your own profile, CV, target companies | **Nowhere.** These stay in your gitignored local files |

Note that GitHub points a new PR from a fork at the upstream repository by default. Check
the "base repository" dropdown before you submit.

## Rules this fork enforces

- **No personal data in tracked files.** Everything `/setup` personalizes is gitignored and
  has a tracked `*.example` twin with `[YOUR_*]` placeholders. `tools/security_guards.py`
  pins the ignore rules, and the CI `placeholder-integrity` job fails if a personal path is
  staged. Never weaken either, and never `git add -f` an ignored file.
- **Fixtures are anonymous.** Test data uses `Acme`-style companies and obviously fake
  contact details (for example `+41 79 000 12 34`).
- **Stdlib only** for Python under `tools/`; zero runtime dependencies for portal CLIs.
- **Portal-skill contract:** `search` / `detail` commands, `--format json|table|plain`,
  stderr JSON errors with exit 1, backoff on 429/5xx, an `enabled:` flag in `SKILL.md`,
  offline tests. `linkedin-search` is the reference; `/add-portal` scaffolds new ones.
- **Personal use only** for ToS-restricted sources. CI never makes live portal requests.

## Before you push

```bash
python3 -m unittest discover -s tests
python3 tools/lint_skills.py
python3 tools/security_guards.py
(cd .agents/skills/<touched-cli>/cli && bun run typecheck && bun test)
```

LaTeX changes must keep the fixtures in `tests/fixtures/latex/` compiling to exactly one
page each (`pdflatex` for the CV, `xelatex` from `cover_letters/` for the letter).
