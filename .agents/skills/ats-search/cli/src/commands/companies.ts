// `companies` - read the registry, and close the loop back from the board.
//
// `--suggest` is the maintenance half: it reads the jobs you actually starred and
// tells you which of those employers are not being monitored yet. That is how a
// company like Rivero - already in the board, never in the registry - gets found
// without you remembering to add it.

import { join } from "path"
import { ROOT, RegistryError, now, readJson, writeError } from "../helpers.ts"
import { cadenceFor, loadRegistry, selectDue, type Company, type Registry } from "../registry.ts"

export interface CompaniesOpts {
  mode: "list" | "due" | "suggest"
  maxCompanies: number
  format: "json" | "table"
}

interface SeenEntry {
  company?: string
  title?: string
  user_status?: string
  portal?: string
  first_seen?: string
}

/** Statuses that mean "I want more of this" - the signal `--suggest` runs on. */
const INTERESTING = new Set(["star", "yes", "maybe", "applied"])

function normalize(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "")
}

function knownNames(registry: Registry): Set<string> {
  const names = new Set<string>()
  for (const c of registry.companies) {
    names.add(normalize(c.name))
    for (const a of c.aliases ?? []) names.add(normalize(a))
  }
  return names
}

export interface Suggestion {
  company: string
  count: number
  statuses: Record<string, number>
  examples: string[]
}

export function suggestFrom(registry: Registry, seen: Record<string, SeenEntry>): Suggestion[] {
  const known = knownNames(registry)
  const byCompany = new Map<string, Suggestion>()
  for (const entry of Object.values(seen)) {
    const raw = (entry.company ?? "").trim()
    if (!raw) continue
    const status = entry.user_status ?? "new"
    if (!INTERESTING.has(status)) continue
    if (known.has(normalize(raw))) continue
    const hit = byCompany.get(normalize(raw)) ?? { company: raw, count: 0, statuses: {}, examples: [] }
    hit.count++
    hit.statuses[status] = (hit.statuses[status] ?? 0) + 1
    if (hit.examples.length < 3 && entry.title) hit.examples.push(entry.title)
    byCompany.set(normalize(raw), hit)
  }
  return [...byCompany.values()].sort((a, b) => b.count - a.count || a.company.localeCompare(b.company))
}

export function runCompanies(opts: CompaniesOpts): number {
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

  if (opts.mode === "suggest") {
    const path = process.env.ATS_SEEN_JOBS || join(ROOT, "job_scraper", "seen_jobs.json")
    const state = readJson<{ seen?: Record<string, SeenEntry> }>(path)
    if (!state?.seen) {
      writeError(`no job board state at ${path} - nothing to suggest from yet`, "NO_STATE")
      return 1
    }
    const suggestions = suggestFrom(registry, state.seen)
    if (opts.format === "json") {
      process.stdout.write(JSON.stringify({ meta: { count: suggestions.length }, results: suggestions }, null, 2) + "\n")
    } else if (!suggestions.length) {
      process.stdout.write("every company you have starred is already in the registry\n")
    } else {
      for (const s of suggestions) {
        const statuses = Object.entries(s.statuses).map(([k, v]) => `${v} ${k}`).join(", ")
        process.stdout.write(`${String(s.count).padStart(3)}  ${s.company}  (${statuses})\n`)
        for (const e of s.examples) process.stdout.write(`       ${e}\n`)
      }
      process.stdout.write(`\n${suggestions.length} companies in your board are not in companies.json\n`)
    }
    return 0
  }

  const nowISO = now().toISOString()
  let rows: Company[]
  let heading: string
  if (opts.mode === "due") {
    const selection = selectDue(registry, nowISO, opts.maxCompanies)
    rows = selection.due
    heading = `${rows.length} companies would be fetched by the next click (${selection.deferred.length} more due, ${selection.skipped.length} not eligible)`
  } else {
    rows = [...registry.companies].sort((a, b) => a.tier - b.tier || a.name.localeCompare(b.name))
    heading = `${rows.length} companies in the registry`
  }

  if (opts.format === "json") {
    process.stdout.write(
      JSON.stringify(
        {
          meta: { count: rows.length, mode: opts.mode },
          results: rows.map((c) => ({
            name: c.name,
            tier: c.tier,
            route: c.route,
            status: c.status,
            vendor: c.vendor ?? null,
            token: c.token ?? null,
            cadence_days: cadenceFor(c, registry.defaults),
            last_success_at: c.last_success_at ?? null,
            last_status: c.last_status ?? null,
            stats: c.stats ?? null,
            flags: c.flags ?? [],
          })),
        },
        null,
        2,
      ) + "\n",
    )
    return 0
  }

  const head = ["tier", "company", "route", "status", "board", "cad", "last ok", "eligible"]
  const body = rows.map((c) => [
    String(c.tier),
    c.name,
    c.route,
    c.status,
    c.vendor && c.token ? `${c.vendor}:${c.token}` : "-",
    String(cadenceFor(c, registry.defaults)),
    (c.last_success_at ?? "-").slice(0, 10),
    String(c.stats?.eligible_jobs ?? 0),
  ])
  const widths = head.map((h, i) => Math.max(h.length, ...body.map((b) => b[i].length)))
  const line = (cells: string[]) => cells.map((c, i) => c.padEnd(widths[i])).join("  ").trimEnd()
  process.stdout.write(line(head) + "\n" + widths.map((w) => "-".repeat(w)).join("  ") + "\n")
  for (const b of body) process.stdout.write(line(b) + "\n")
  process.stdout.write(`\n${heading}\n`)
  return 0
}
