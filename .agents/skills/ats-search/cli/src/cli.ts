#!/usr/bin/env bun
// ats-search: monitor a hand-maintained list of target companies directly on
// their ATS boards. Zero runtime dependencies - bun + fetch + regex.
//
// This skill honors the /add-portal portal contract with three documented
// deviations, because ATS boards are a whole-board source rather than a
// keyword-driven portal:
//
//   1. `search` takes no required query and issues ONE batch call per run.
//      `-q` filters what was already fetched; it never multiplies requests (§10.1).
//   2. Result ids are composite - `<vendor>:<token>:<posting_id>` (§10.2).
//   3. Partial failure exits 0 and reports `meta.degraded`, so a run never throws
//      away the companies that did succeed (§10.3).

import { runSearch, type SearchOpts } from "./commands/search.ts"
import { runDetail, type DetailOpts } from "./commands/detail.ts"
import { runResolve, type ResolveOpts } from "./commands/resolve.ts"
import { runCompanies, type CompaniesOpts } from "./commands/companies.ts"
import { registryPath } from "./registry.ts"
import { readFileSync } from "fs"

/**
 * Read the caller's already-known composite ids. Accepts a JSON array or one id
 * per line, because both are trivial to produce from a shell and from Python.
 */
function loadKnownIds(path: string | undefined): Set<string> {
  if (!path) return new Set()
  const raw = readFileSync(path, "utf-8").trim()
  if (!raw) return new Set()
  if (raw.startsWith("[")) {
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) throw new Error("expected a JSON array of ids")
    return new Set(parsed.map(String))
  }
  if (raw.startsWith("{")) {
    // Reading a JSON object as one long "id" would silently produce an empty
    // known set, and an empty known set makes every row look new.
    throw new Error("expected a JSON array or one id per line, got a JSON object")
  }
  return new Set(raw.split("\n").map((l) => l.trim()).filter(Boolean))
}

interface Flags {
  _: string[]
  [k: string]: string | boolean | string[]
}

const ALIAS: Record<string, string> = { q: "query", n: "limit", c: "company" }
/** Flags that may repeat and collect into a list. */
const REPEATABLE = new Set(["company"])

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (!a.startsWith("-")) {
      ;(flags._ as string[]).push(a)
      continue
    }
    const name = a.replace(/^-+/, "")
    const key = ALIAS[name] ?? name
    const next = argv[i + 1]
    let value: string | boolean = true
    if (next !== undefined && !next.startsWith("-")) {
      value = next
      i++
    }
    if (REPEATABLE.has(key)) {
      const acc = Array.isArray(flags[key]) ? (flags[key] as string[]) : []
      if (typeof value === "string") acc.push(value)
      flags[key] = acc
    } else {
      flags[key] = value
    }
  }
  return flags
}

function badArg(message: string): number {
  process.stderr.write(JSON.stringify({ error: message, code: "BAD_ARGS" }) + "\n")
  return 1
}

/**
 * Every flag each subcommand accepts. An unrecognized flag is an error, not a
 * shrug: `--max-compaines 1` silently accepted means a run that was supposed to
 * touch one company touches eight, and you find out from the request log.
 */
const KNOWN_FLAGS: Record<string, string[]> = {
  search: ["query", "company", "max-companies", "max-new-jobs", "known-ids", "limit", "jobage",
           "page", "format", "force-refresh", "no-write"],
  detail: ["format"],
  resolve: ["company", "all-unresolved", "max-companies", "max-probes", "dry-run", "format"],
  companies: ["list", "due", "suggest", "max-companies", "format"],
}
const UNIVERSAL_FLAGS = ["help", "h"]

function checkFlags(cmd: string, flags: Flags): string | null {
  const known = new Set([...(KNOWN_FLAGS[cmd] ?? []), ...UNIVERSAL_FLAGS])
  const unknown = Object.keys(flags).filter((k) => k !== "_" && !known.has(k))
  if (!unknown.length) return null
  return `unknown flag${unknown.length > 1 ? "s" : ""} for \`${cmd}\`: ${unknown.map((u) => "--" + u).join(", ")}. ` +
    `Accepted: ${[...known].sort().map((k) => "--" + k).join(", ")}`
}

/**
 * A whole non-negative integer, or an error. Deliberately stricter than
 * `parseInt`, which reads "8companies" as 8 and "1e3" as 1 - a typo that quietly
 * changes a request bound is exactly the failure this guards.
 */
function intFlag(flags: Flags, name: string, fallback: number): number | null {
  const raw = flags[name]
  if (raw === undefined) return fallback
  if (typeof raw !== "string" || !/^\d+$/.test(raw.trim())) {
    badArg(`--${name} must be a whole number, got ${JSON.stringify(raw === true ? "" : raw)}`)
    return null
  }
  return parseInt(raw.trim(), 10)
}

const HELP = `ats-search — watch your target companies' own ATS boards (Greenhouse, Ashby, Personio, Lever, SmartRecruiters)

USAGE
  bun run src/cli.ts search    [-q "<text>"] [--company <name>] [--max-companies N] [--limit N]
  bun run src/cli.ts detail    <vendor:token:posting_id | posting-url> [--format json|plain]
  bun run src/cli.ts resolve   (--company <name> | --all-unresolved) [--dry-run]
  bun run src/cli.ts companies [--list | --due | --suggest] [--format json|table]

SEARCH — one batch call over the companies whose cadence is up
  --query, -q <text>      LOCAL filter over what was already fetched (OR over terms).
                          It never issues an extra request; there is no per-query fan-out.
  --company, -c <name>    Fetch these companies instead of the due ones. Repeatable.
                          Accepts a registry name, an alias, or "vendor:token".
  --max-companies <n>     Companies per run. Default 8.
  --max-new-jobs <n>      Stop selecting further companies once this many NEW rows have
                          accumulated. Default 0 (unbounded). The company in flight is
                          always finished in full; unreached companies keep their old
                          last_success_at and come first next run.
  --known-ids <file>      Composite ids already in your board, one per line or a JSON
                          array. What makes --max-new-jobs count *new* rows rather than
                          eligible ones - without it every eligible row looks new, and a
                          company whose whole board you have already seen would eat the
                          budget. tools/ats_fetch.py writes this file for you.
  --limit, -n <n>         Cap on returned rows after sorting. Default 0 (no cap).
  --jobage <days>         Drop postings older than N days. A posting with no date is kept.
  --page <n>              Accepted for contract compatibility; only page 1 has results.
  --format <fmt>          json (default) | table | plain
  --force-refresh         Ignore the per-company cooldown cache. Debugging only.
  --no-write              Do not write fetch bookkeeping back into the registry.

DETAIL
  <id>                    A composite id from a search result, or a posting URL.
                          A bare vendor posting id is rejected (AMBIGUOUS_ID): it is
                          not unique across boards and is never guessed at.

RESOLVE — find a board and decide whether we know it is really theirs
  --company, -c <name>    Resolve these companies. Repeatable.
  --all-unresolved        Resolve every company whose status is "unresolved".
  --max-companies <n>     Cap per run. Default 12 (one Phase 0 batch).
  --max-probes <n>        Slug-fallback probes per company. Default 12.
  --dry-run               Report, write nothing.

COMPANIES
  --list                  The whole registry (default).
  --due                   Exactly what the next search would fetch, and why.
  --suggest               Employers you starred in the job board that are not in the registry.

REGISTRY
  ${registryPath()}
  Override with ATS_REGISTRY. Copy job_scraper/companies.example.json to start one.

Errors go to stderr as {"error","code"}. Exit 0 when at least one company
succeeded (with meta.degraded when some did not), 1 when all of them failed.
`

async function main(): Promise<number> {
  const argv = process.argv.slice(2)
  const flags = parseFlags(argv)
  const cmd = (flags._ as string[])[0]

  if (!cmd || flags.help || flags.h) {
    process.stdout.write(HELP)
    return cmd ? 0 : 1
  }

  if (!KNOWN_FLAGS[cmd]) {
    return badArg(`unknown command "${cmd}" - expected search, detail, resolve or companies`)
  }
  const flagError = checkFlags(cmd, flags)
  if (flagError) return badArg(flagError)

  const rawFormat = typeof flags.format === "string" ? flags.format : ""

  if (cmd === "search") {
    if (rawFormat && !["json", "table", "plain"].includes(rawFormat)) {
      return badArg(`--format must be json, table or plain, got "${rawFormat}"`)
    }
    const maxCompanies = intFlag(flags, "max-companies", 8)
    if (maxCompanies === null) return 1
    const maxNewJobs = intFlag(flags, "max-new-jobs", 0)
    if (maxNewJobs === null) return 1
    const knownIdsPath = typeof flags["known-ids"] === "string" ? flags["known-ids"] : undefined
    let knownIds: Set<string>
    try {
      knownIds = loadKnownIds(knownIdsPath)
    } catch (e) {
      return badArg(`--known-ids: ${e instanceof Error ? e.message : String(e)}`)
    }
    const limit = intFlag(flags, "limit", 0)
    if (limit === null) return 1
    const page = intFlag(flags, "page", 1)
    if (page === null) return 1
    const jobage = flags.jobage === undefined ? null : intFlag(flags, "jobage", 0)
    if (jobage === null && flags.jobage !== undefined) return 1

    const opts: SearchOpts = {
      query: typeof flags.query === "string" ? flags.query : undefined,
      only: Array.isArray(flags.company) ? flags.company : [],
      maxCompanies,
      maxNewJobs,
      knownIds,
      limit,
      page: Math.max(1, page),
      jobage,
      format: (rawFormat || "json") as SearchOpts["format"],
      forceRefresh: flags["force-refresh"] === true,
      noWrite: flags["no-write"] === true,
    }
    return runSearch(opts)
  }

  if (cmd === "detail") {
    const id = (flags._ as string[])[1]
    if (!id) return badArg("detail requires <vendor:token:posting_id> or a posting URL")
    if (rawFormat && !["json", "plain"].includes(rawFormat)) {
      return badArg(`--format must be json or plain, got "${rawFormat}"`)
    }
    const opts: DetailOpts = { id, format: rawFormat === "plain" ? "plain" : "json" }
    return runDetail(opts)
  }

  if (cmd === "resolve") {
    const maxCompanies = intFlag(flags, "max-companies", 12)
    if (maxCompanies === null) return 1
    const maxProbes = intFlag(flags, "max-probes", 12)
    if (maxProbes === null) return 1
    const opts: ResolveOpts = {
      only: Array.isArray(flags.company) ? flags.company : [],
      allUnresolved: flags["all-unresolved"] === true,
      maxCompanies,
      maxProbes,
      dryRun: flags["dry-run"] === true,
      format: rawFormat === "json" ? "json" : "table",
    }
    return runResolve(opts)
  }

  if (cmd === "companies") {
    const maxCompanies = intFlag(flags, "max-companies", 8)
    if (maxCompanies === null) return 1
    const mode: CompaniesOpts["mode"] = flags.suggest ? "suggest" : flags.due ? "due" : "list"
    return runCompanies({ mode, maxCompanies, format: rawFormat === "json" ? "json" : "table" })
  }

  return badArg(`unknown command "${cmd}" - expected search, detail, resolve or companies`)
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    process.stderr.write(
      JSON.stringify({ error: e instanceof Error ? e.message : String(e), code: "INTERNAL_ERROR" }) + "\n",
    )
    process.exit(1)
  })
