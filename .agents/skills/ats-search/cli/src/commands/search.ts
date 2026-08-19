// `search` - one batch call over the due companies, filtered locally (§10.1).
//
// This command takes no required query. It reads the registry, selects the
// companies whose cadence is up, fetches each board exactly once, and filters in
// process. `-q` is a *local* OR filter over what was already fetched; it never
// multiplies requests.

import { join } from "path"
import {
  RegistryError,
  cacheDir,
  compositeId,
  daysBetween,
  now,
  readJson,
  stableSort,
  todayISO,
  transport,
  writeError,
  writeJsonAtomic,
} from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import { applyFilters, localQuery, type FilteredPosting } from "../filters.ts"
import { prefit } from "../score.ts"
import {
  cadenceFor,
  filterConfigFor,
  loadRegistry,
  selectDue,
  vendorAccess,
  writeBookkeeping,
  type Bookkeeping,
  type Company,
  type Registry,
} from "../registry.ts"
import { adapterFor } from "../vendors/index.ts"
import type { BoardResult, CompanyReport, JobResult, SearchPayload } from "../types.ts"

export interface SearchOpts {
  query?: string
  /** Explicit company names or `vendor:token` pairs; empty means "whatever is due". */
  only: string[]
  maxCompanies: number
  /**
   * Stop *selecting* companies once this many **new** rows have accumulated.
   * 0 = unbounded.
   *
   * Not a truncation point (§2): the company being processed is always finished
   * in full, then no further company is selected - a half-ingested company would
   * leave a state where a later run cannot tell "not fetched" from "fetched and
   * filtered out".
   *
   * "New" means "not in `knownIds`", which the caller supplies via
   * `--known-ids`. Counting *eligible* rows instead looked conservative and was
   * not: a first company whose 40 eligible postings are all already in the board
   * would exhaust the budget having added nothing, and defer the company that
   * actually had new work. The bound has to mean what §2 says it means.
   */
  maxNewJobs: number
  /** Composite ids already in the caller's board, from `--known-ids <file>`. */
  knownIds: Set<string>
  limit: number
  page: number
  jobage: number | null
  format: "json" | "table" | "plain"
  /** Skip the §2 per-company cooldown cache. Manual debugging only. */
  forceRefresh: boolean
  /** Leave the registry untouched (tests, dry runs). */
  noWrite: boolean
}

/** Statuses that mean "this company's data is trustworthy and complete". */
const SUCCESS = new Set(["ok", "empty"])

interface CacheEntry {
  fetched_at: string
  board: BoardResult
}

/**
 * The §2 per-company cooldown: a board fetched inside `min_interval_minutes` is
 * served from the last run's payload. This is the hard backstop against any
 * caller - a mashed button, or a `/scrape` that fans out - turning N companies
 * into 9N requests.
 */
function cached(company: Company, minutes: number): CacheEntry | null {
  if (minutes <= 0) return null
  const path = join(cacheDir(), `${company.vendor}-${company.token}.json`)
  const entry = readJson<CacheEntry>(path)
  if (!entry?.fetched_at || !entry.board) return null
  const ageMin = (now().getTime() - Date.parse(entry.fetched_at)) / 60000
  return ageMin >= 0 && ageMin < minutes ? entry : null
}

function putCache(company: Company, board: BoardResult): void {
  writeJsonAtomic(join(cacheDir(), `${company.vendor}-${company.token}.json`), {
    fetched_at: now().toISOString(),
    board,
  })
}

/**
 * Resolve one `--company` value, most specific form first: a registry name or
 * alias, then `vendor:token`, then a bare token. The precedence matters -
 * "Parloa" is a company name *and* another entry's board token, and matching
 * both would silently fetch the same board twice and double-count the run.
 */
export function resolveSelector(companies: Company[], selector: string): Company[] {
  const s = selector.trim().toLowerCase()
  if (!s) return []
  const byName = companies.filter(
    (c) => c.name.toLowerCase() === s || (c.aliases ?? []).some((a) => a.toLowerCase() === s),
  )
  if (byName.length) return byName
  const byPair = companies.filter((c) => c.token && `${c.vendor}:${c.token}`.toLowerCase() === s)
  if (byPair.length) return byPair
  return companies.filter((c) => c.token && c.token.toLowerCase() === s)
}

function toResult(item: FilteredPosting, company: Company, today: string, cities: string[]): JobResult {
  const p = item.posting
  const matched = item.matched
  const { score, reasons } = prefit(item, company.tier, today, { cities })
  return {
    id: compositeId(company.vendor!, company.token!, p.posting_id),
    title: p.title,
    // A venture studio's board (Merantix) carries the portfolio company on the
    // posting; everywhere else the registry name is the employer.
    company: p.company || company.name,
    registry_company: company.name,
    location: matched?.raw || (p.locations[0]?.text ?? null),
    date: p.date,
    url: p.url,
    vendor: company.vendor!,
    token: company.token!,
    tier: company.tier,
    locations: p.locations.map((l) => l.text),
    city: matched?.city ?? null,
    country: matched?.country ?? null,
    region: matched?.region ?? null,
    workplace: item.workplace,
    location_uncertain: item.location_uncertain,
    updated_at: p.updated_at,
    deadline: p.deadline,
    description: p.description,
    prefit_score: score,
    prefit_reasons: reasons,
    flags: item.flags,
  }
}

export async function runSearch(opts: SearchOpts, get: Transport = transport()): Promise<number> {
  let registry: Registry
  try {
    registry = loadRegistry()
  } catch (e) {
    if (e instanceof RegistryError) {
      writeError(e.message, "BAD_REGISTRY")
      return 1
    }
    throw e
  }

  const today = todayISO()
  const nowISO = now().toISOString()

  let selected: Company[]
  let deferred: Company[] = []
  const notes: string[] = []
  if (opts.only.length) {
    const picked = new Map<string, Company>()
    const unknown: string[] = []
    for (const selector of opts.only) {
      const matches = resolveSelector(registry.companies, selector)
      if (!matches.length) {
        unknown.push(selector)
        continue
      }
      if (matches.length > 1) {
        writeError(
          `"${selector}" matches ${matches.length} registry entries (${matches.map((m) => m.name).join(", ")}) - name them individually`,
          "BAD_ARGS",
        )
        return 1
      }
      picked.set(matches[0].name, matches[0])
    }
    if (unknown.length) {
      writeError(`no registry entry matches: ${unknown.join(", ")}`, "BAD_ARGS")
      return 1
    }
    // Naming a company on the command line chooses *which* companies to fetch.
    // It does not lift any of the rules about whether a company may be fetched:
    // identity, published access policy and the request bound all still apply.
    const refused: string[] = []
    const fetchable: Company[] = []
    for (const company of picked.values()) {
      if (company.route !== "ats" || !company.vendor || !company.token) {
        refused.push(`${company.name}: route ${company.route}, no board configured`)
        continue
      }
      if (company.status !== "verified") {
        // §8: an unverified board may be *probed* by `resolve`, which is what it
        // is for. Ingesting from it would put another company's postings in your
        // board under this company's name.
        refused.push(
          `${company.name}: status ${company.status} - run \`resolve -c "${company.name}"\` first; ` +
            `postings are only ingested from a board whose identity is verified`,
        )
        continue
      }
      const access = vendorAccess(registry.defaults, company.vendor)
      if (!access.enabled) {
        refused.push(
          `${company.name}: vendor ${company.vendor} is disabled${access.why ? ` - ${access.why}` : ""}`,
        )
        continue
      }
      fetchable.push(company)
    }
    if (refused.length) notes.push(refused.join("; "))
    if (!fetchable.length) {
      // Exiting 0 with an empty result set here would read as "asked, found
      // nothing" - which is exactly wrong when the answer is "refused to ask".
      writeError(
        `no fetchable company among the ${opts.only.length} requested: ${refused.join("; ")}`,
        "NO_FETCHABLE_COMPANY",
      )
      return 1
    }
    // The request bound is not bypassable by listing companies by hand either.
    selected = fetchable.slice(0, Math.max(1, opts.maxCompanies))
    deferred = fetchable.slice(Math.max(1, opts.maxCompanies))
  } else {
    const selection = selectDue(registry, nowISO, opts.maxCompanies)
    selected = selection.due
    deferred = selection.deferred
  }

  // §10.1: the whole board is already in hand, so only page 1 is meaningful.
  // `--page 2` is answered with an empty set and a note, never an error.
  if (opts.page > 1) {
    const payload: SearchPayload = {
      meta: {
        count: 0,
        page: opts.page,
        degraded: false,
        status_counts: {},
        companies: [],
        requests: 0,
        new_rows: 0,
        generated_at: nowISO,
        note: "ats-search fetches whole boards, so only page 1 carries results; --page is accepted for contract compatibility only",
      },
      results: [],
    }
    emit(payload, opts.format)
    return 0
  }

  const minInterval = registry.defaults.min_interval_minutes ?? 60
  const reports: CompanyReport[] = []
  const bookkeeping: Bookkeeping[] = []
  let results: JobResult[] = []
  let requests = 0
  let newRows = 0

  const notReached: Company[] = []
  for (const [index, company] of selected.entries()) {
    // The stop-selecting rule. Checked before the company is touched, so an
    // unreached company gets no request, no bookkeeping, and keeps its old
    // last_success_at - which is what puts it first in the next run's queue.
    if (opts.maxNewJobs > 0 && newRows >= opts.maxNewJobs) {
      notReached.push(...selected.slice(index))
      break
    }
    const adapter = adapterFor(company.vendor!)
    if (!adapter) {
      reports.push({
        name: company.name, vendor: null, token: company.token ?? null,
        status: "schema_changed", message: `no adapter for vendor ${company.vendor}`,
        jobs_seen: 0, eligible: 0, requests: 0, cached: false,
      })
      continue
    }
    const hit = opts.forceRefresh ? null : cached(company, minInterval)
    const board = hit ? hit.board : await adapter.fetchBoard(company.token!, get)
    if (!hit && SUCCESS.has(board.status)) putCache(company, board)
    requests += hit ? 0 : board.requests

    const cfg = filterConfigFor(company, registry.defaults)
    const outcome = applyFilters(board.postings, cfg, { jobageDays: opts.jobage, today })
    let kept = outcome.kept
    if (opts.query) kept = kept.filter((item) => localQuery(item, opts.query!))
    const rows = kept.map((item) => toResult(item, company, today, cfg.cities))
    results.push(...rows)
    newRows += rows.filter((r) => !opts.knownIds.has(r.id)).length

    reports.push({
      name: company.name,
      vendor: company.vendor!,
      token: company.token!,
      status: board.status,
      message: board.message,
      jobs_seen: board.postings.length,
      eligible: rows.length,
      requests: hit ? 0 : board.requests,
      cached: Boolean(hit),
    })
    bookkeeping.push({
      name: company.name,
      attempted_at: nowISO,
      status: board.status,
      // `success_partial` deliberately does NOT move last_success_at: the board is
      // incomplete, so the next click should pick this company up first.
      success: SUCCESS.has(board.status),
      jobs_seen: board.postings.length,
      eligible: rows.length,
    })
  }

  const statusCounts: Record<string, number> = {}
  for (const r of reports) statusCounts[r.status] = (statusCounts[r.status] ?? 0) + 1
  const failed = reports.filter((r) => !SUCCESS.has(r.status))
  const succeeded = reports.filter((r) => SUCCESS.has(r.status))

  results = stableSort(results)
  const limited = opts.limit > 0 ? results.slice(0, opts.limit) : results

  if (notReached.length) {
    notes.push(
      `${notReached.length} selected companies were not reached: --max-new-jobs ${opts.maxNewJobs} was met after ` +
        `${reports.length} companies. They keep their old last_success_at and come first next run.`,
    )
  }
  if (deferred.length) {
    notes.push(
      `${deferred.length} more due but past --max-companies; they keep their old last_success_at and are picked first next run`,
    )
  }
  for (const company of selected) {
    const cadence = cadenceFor(company, registry.defaults)
    if (company.last_success_at && daysBetween(company.last_success_at, today) > cadence * 4) {
      notes.push(`${company.name} has not been fetched successfully in ${daysBetween(company.last_success_at, today)} days`)
    }
  }

  const payload: SearchPayload = {
    meta: {
      count: limited.length,
      page: 1,
      degraded: failed.length > 0 && succeeded.length > 0,
      status_counts: statusCounts,
      companies: reports,
      requests,
      new_rows: newRows,
      generated_at: nowISO,
      ...(notes.length ? { note: notes.join("; ") } : {}),
    },
    results: limited,
  }

  if (!opts.noWrite) {
    try {
      writeBookkeeping(bookkeeping)
    } catch (e) {
      // Losing the bookkeeping write is a nuisance, not a reason to throw away a
      // run's results - the whole point of §10.3 is that good data survives.
      payload.meta.note = [payload.meta.note, `registry bookkeeping not written: ${e instanceof Error ? e.message : String(e)}`]
        .filter(Boolean)
        .join("; ")
    }
  }

  // Every selected company failed: exit 1, but still print meta.companies on
  // stdout so the caller can see *why* without re-running.
  if (reports.length > 0 && succeeded.length === 0) {
    emit(payload, opts.format)
    writeError(
      `all ${reports.length} selected companies failed: ${failed.map((f) => `${f.name} ${f.status}`).join(", ")}`,
      "ALL_COMPANIES_FAILED",
    )
    return 1
  }

  emit(payload, opts.format)
  return 0
}

function emit(payload: SearchPayload, format: SearchOpts["format"]): void {
  if (format === "json") {
    process.stdout.write(JSON.stringify(payload, null, 2) + "\n")
    return
  }
  if (format === "table") {
    const rows = payload.results
    if (!rows.length) {
      process.stdout.write("(no results)\n")
    } else {
      const head = ["fit", "date", "title", "company", "location"]
      const body = rows.map((r) => [
        String(r.prefit_score),
        r.date ?? "-",
        r.title.slice(0, 46),
        (r.company ?? "").slice(0, 20),
        (r.location ?? "").slice(0, 22),
      ])
      const widths = head.map((h, i) => Math.max(h.length, ...body.map((b) => b[i].length)))
      const line = (cells: string[]) => cells.map((c, i) => c.padEnd(widths[i])).join("  ").trimEnd()
      process.stdout.write(line(head) + "\n")
      process.stdout.write(widths.map((w) => "-".repeat(w)).join("  ") + "\n")
      for (const b of body) process.stdout.write(line(b) + "\n")
    }
  } else {
    for (const r of payload.results) {
      process.stdout.write(`${r.title}\n  ${r.company ?? ""} - ${r.location ?? ""} - ${r.date ?? "no date"}\n  ${r.url}\n\n`)
    }
  }
  const m = payload.meta
  process.stdout.write(
    `\n${m.count} results from ${m.companies.length} companies, ${m.requests} requests` +
      (m.degraded ? " (DEGRADED - see --format json for per-company status)" : "") +
      "\n",
  )
  for (const c of m.companies) {
    if (!SUCCESS.has(c.status)) process.stdout.write(`  ! ${c.name}: ${c.status}${c.message ? ` - ${c.message}` : ""}\n`)
  }
  if (m.note) process.stdout.write(`  note: ${m.note}\n`)
}
