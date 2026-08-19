# `ats-search` CLI

Watch a hand-maintained list of target companies on their **own** ATS boards.
Zero runtime dependencies — `bun` + `fetch` + regex. No API key, no account.

```bash
bun install                                   # dev types only
bun run typecheck
bun test                                      # all offline except one live smoke test
bun run src/cli.ts search --format table
```

## Commands

```bash
bun run src/cli.ts search    [-q "<text>"] [-c <company>] [--max-companies N] [--max-new-jobs N] [--known-ids FILE] [--limit N]
bun run src/cli.ts detail    <vendor:token:posting_id | posting-url> [--format json|plain]
bun run src/cli.ts resolve   (-c <company> | --all-unresolved) [--dry-run]
bun run src/cli.ts companies [--list | --due | --suggest]
```

Full flag reference: `bun run src/cli.ts --help`, and the skill docs in
`../SKILL.md`.

## Three deviations from the portal-skill contract, on purpose

| Contract says | Here | Why |
|---|---|---|
| `search -q <keywords>` drives the run | `search` takes **no required query**; one call fetches the due boards and filters locally. `-q` filters what was already fetched. | An ATS board is a whole-board source. Translating query categories into per-query invocations would multiply requests by the number of categories for zero extra coverage. |
| `id` is the portal's posting id | `id` is `<vendor>:<token>:<posting_id>` | A bare vendor id is not unique across ~55 companies and five vendors. `detail` rejects a bare id with `AMBIGUOUS_ID` rather than guessing which board it came from. |
| Non-zero exit on failure | Exit **0** when at least one company succeeded, with `meta.degraded: true` and a per-company status table | `tools/scrape_cron.py`'s `bun()` returns `None` on any non-zero exit and ignores stdout, so "finish the others, then exit non-zero" would throw away every company that worked. |

## Layout

```
src/
  cli.ts              arg parsing, help, dispatch
  helpers.ts          transport (backoff, per-host serialization, spacing), ids, sort, text, atomic writes
  registry.ts         companies.json: load/validate, cadence selection, bookkeeping write-back
  filters.ts          pure: titles, the location model, conditional excludes
  score.ts            pure: the deterministic `prefit` display prior
  types.ts            the shapes every layer agrees on
  vendors/            greenhouse ashby personio lever smartrecruiters (+ vendor.ts, the adapter contract)
  commands/           search detail resolve companies
tests/
  fixtures/           dated vendor payloads + map.json + capture.sh
```

## Testing offline

The transport is swapped for a fixture map when `ATS_FIXTURES` points at a
directory containing `map.json`. Every environment hook the tests use:

| Env var | Purpose |
|---|---|
| `ATS_REGISTRY` | Use a different `companies.json`. |
| `ATS_FIXTURES` | Answer every request from this fixture directory. |
| `ATS_REQUEST_LOG` | Append every requested URL here — how "`-q` issues no extra request" is actually asserted. |
| `ATS_CACHE_DIR` | Where the per-company cooldown cache lives. |
| `ATS_NOW` | Freeze the clock, so cadence and recency are deterministic. |
| `ATS_SEEN_JOBS` | Point `companies --suggest` at a different job board state. |
| `ATS_SR_PAGE_SIZE` | SmartRecruiters page size — lets a 3-posting fixture exercise pagination. |

Test scratch (throwaway registries and caches) is written to `.tmp-tests/` inside
this package rather than `os.tmpdir()`: a sandboxed CI or review environment can
have `/tmp` read-only, and a suite that cannot run is a suite that stops catching
things.

Refresh fixtures with `tests/fixtures/capture.sh`, then minimize the payloads and
update each file's `_capture` block. A vendor schema change should be a dated
diff, not an archaeology exercise.

## Access

`../url-reference.md` records the `robots.txt` verdict and terms link for each
vendor host, with the date checked. **`api.smartrecruiters.com` disallows every
agent but LinkedInBot**, so that vendor is fail-closed: the policy is compiled
into `src/registry.ts` (`POLICY_BLOCKED`), the registry block is belt-and-braces,
and neither `search -c` nor `detail` can get round it.

A host that answers 429 is stopped for the rest of the day, with no retry, in a
ledger next to the payload cache — so the stop holds across commands and across
runs, not just inside one loop.
