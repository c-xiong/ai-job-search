// `job_scraper/companies.json` - the master target list. Hand-editable, and the
// one file both the TypeScript CLI and the Python tools read with no parsing code
// (the precedent is `job_scraper/scrape_config.json`).
//
// The CLI also writes back, but only the bookkeeping fields: last_attempt_at,
// last_success_at, last_status and stats. Everything else - who you are watching,
// at what cadence, with what identity evidence - is yours, and a run that touched
// it would be a bug.

import { closeSync, existsSync, openSync, readFileSync, unlinkSync, writeFileSync } from "fs"
import { dirname, join } from "path"
import { RegistryError, ROOT, writeJsonAtomic } from "./helpers.ts"
import type { CompanyStatus, VendorName } from "./types.ts"
import { VENDORS } from "./types.ts"
import type { ConditionalExclude, FilterConfig } from "./filters.ts"

export const RESOLUTION_STATUSES = [
  "verified",
  "unresolved",
  "ambiguous",
  "unsupported_vendor",
  "no_public_board",
  "paused",
] as const
export type ResolutionStatus = (typeof RESOLUTION_STATUSES)[number]

export const ROUTES = ["ats", "linkedin", "manual"] as const
export type Route = (typeof ROUTES)[number]

/** The Python board uses this same O_EXCL lease; flock and O_EXCL do not interoperate. */
function withRegistryLock(path: string, action: () => void): void {
  const lock = join(dirname(path), ".companies.lock")
  const deadline = Date.now() + 30_000
  let fd: number | undefined
  while (fd === undefined) {
    try {
      fd = openSync(lock, "wx", 0o600)
      writeFileSync(fd, `${process.pid}\n`)
    } catch (error: any) {
      if (error?.code !== "EEXIST") throw error
      if (Date.now() >= deadline) throw new RegistryError("company registry is busy")
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25)
    }
  }
  try { action() } finally {
    closeSync(fd)
    try { unlinkSync(lock) } catch (error: any) { if (error?.code !== "ENOENT") throw error }
  }
}

/** Evidence hierarchy from §8. Only these two promote a board to `verified`. */
export const PROMOTING_EVIDENCE = ["company_site_link", "vendor_identifier", "human_confirmed"] as const

export interface CompanyIdentity {
  evidence_kind: string
  evidence: string
  verified_at: string
}

export interface CompanyStats {
  jobs_seen: number
  german_gated: number
  eligible_jobs: number
  last_eligible_at: string | null
  /** Counts from the most recent board fetch, unlike the cumulative fields above. */
  last_jobs_seen: number | null
  last_eligible_jobs: number | null
}

export interface Company {
  name: string
  aliases?: string[]
  domain?: string
  /** Exact careers page supplied by the user; preferred over guessed paths. */
  careers_url?: string
  tier: number
  vendor?: VendorName | null
  token?: string | null
  status: ResolutionStatus
  route: Route
  cadence_days?: number
  identity?: CompanyIdentity | null
  last_attempt_at?: string | null
  last_success_at?: string | null
  last_status?: string | null
  stats?: CompanyStats
  flags?: string[]
  note?: string
  /** Per-company filter overrides. Required for tier 4 (§5). */
  countries?: string[]
  cities?: string[]
  remote_regions?: string[]
  title_include?: string[]
  title_exclude?: string[]
  /** Candidate boards recorded by `resolve` when identity was insufficient. */
  candidates?: Array<{ vendor: string; token: string; note?: string }>
  /** Detection retry state, written by `resolve` (COMPANIES_PLAN §3.2). */
  resolve_attempts?: number
  last_resolve_at?: string | null
  /** When `resolve --due` may try again; null means stop and ask the owner. */
  next_resolve_at?: string | null
  /** The last detection's explanation while the row is not verified. */
  resolve_detail?: string
  /** Consecutive `not_found` board fetches; 3 sends a verified row back to `resolve`. */
  missing_streak?: number
}

/**
 * Per-vendor access policy, recorded from the robots.txt / terms check rather
 * than assumed. A vendor whose published policy disallows the path is disabled
 * here by default: the check decides, not the plan, and not the code.
 */
export interface VendorAccess {
  enabled: boolean
  robots?: string
  checked?: string
  why?: string
}

export interface RegistryDefaults {
  allowed_countries: string[]
  remote_regions: string[]
  cities: string[]
  title_include: string[]
  title_exclude: string[]
  conditional_exclude: ConditionalExclude[]
  cadence_days: Record<string, number>
  min_interval_minutes: number
  vendor_access?: Record<string, VendorAccess>
}

export interface Registry {
  _comment?: string
  defaults: RegistryDefaults
  companies: Company[]
}

const DEFAULT_DEFAULTS: RegistryDefaults = {
  allowed_countries: ["CH", "DE", "AT"],
  remote_regions: ["europe", "eu", "dach", "emea"],
  cities: [],
  title_include: [],
  title_exclude: [],
  conditional_exclude: [],
  cadence_days: { "1": 1, "2": 1, "3": 3, "4": 7, "5": 14 },
  min_interval_minutes: 60,
  vendor_access: {},
}

export function registryPath(): string {
  return process.env.ATS_REGISTRY || join(ROOT, "job_scraper", "companies.json")
}

function strArray(value: unknown, where: string): string[] {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.some((v) => typeof v !== "string")) {
    throw new RegistryError(`${where} must be an array of strings`)
  }
  return value as string[]
}

/** Load and validate. Every failure here is BAD_REGISTRY and exit 1 (§10.3). */
export function loadRegistry(path = registryPath()): Registry {
  if (!existsSync(path)) {
    throw new RegistryError(
      `registry not found at ${path} - copy job_scraper/companies.example.json to job_scraper/companies.json and add your target companies`,
    )
  }
  let data: unknown
  try {
    data = JSON.parse(readFileSync(path, "utf-8"))
  } catch (e) {
    throw new RegistryError(`registry is not valid JSON (${e instanceof Error ? e.message : String(e)})`)
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new RegistryError("registry must be a JSON object")
  }
  const raw = data as { defaults?: unknown; companies?: unknown; _comment?: string }
  if (!Array.isArray(raw.companies)) throw new RegistryError("registry has no `companies` array")

  const defaults: RegistryDefaults = { ...DEFAULT_DEFAULTS, ...((raw.defaults as object) ?? {}) }
  for (const key of ["allowed_countries", "remote_regions", "cities", "title_include", "title_exclude"] as const) {
    defaults[key] = strArray((defaults as unknown as Record<string, unknown>)[key], `defaults.${key}`)
  }
  if (!Array.isArray(defaults.conditional_exclude)) {
    throw new RegistryError("defaults.conditional_exclude must be an array")
  }

  const seen = new Set<string>()
  const companies: Company[] = []
  for (const [i, entry] of (raw.companies as unknown[]).entries()) {
    const where = `companies[${i}]`
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      throw new RegistryError(`${where} must be an object`)
    }
    const c = entry as Company
    if (!c.name || typeof c.name !== "string") throw new RegistryError(`${where} has no \`name\``)
    if (c.careers_url !== undefined && typeof c.careers_url !== "string") {
      throw new RegistryError(`${c.name}: \`careers_url\` must be a string`)
    }
    if (seen.has(c.name.toLowerCase())) throw new RegistryError(`duplicate company name: ${c.name}`)
    seen.add(c.name.toLowerCase())
    if (typeof c.tier !== "number" || c.tier < 1 || c.tier > 5) {
      throw new RegistryError(`${c.name}: \`tier\` must be a number 1-5`)
    }
    if (!RESOLUTION_STATUSES.includes(c.status as ResolutionStatus)) {
      throw new RegistryError(`${c.name}: unknown status ${JSON.stringify(c.status)}`)
    }
    if (!ROUTES.includes(c.route as Route)) {
      throw new RegistryError(`${c.name}: \`route\` must be one of ${ROUTES.join(", ")}`)
    }
    if (c.vendor && !VENDORS.includes(c.vendor)) {
      throw new RegistryError(`${c.name}: unsupported vendor ${JSON.stringify(c.vendor)}`)
    }
    if (c.route === "ats" && c.status === "verified" && (!c.vendor || !c.token)) {
      throw new RegistryError(`${c.name}: route ats + status verified requires both \`vendor\` and \`token\``)
    }
    // §5: a tier-4 company without its own `countries` would be filtered to
    // nothing by the DACH default - ElevenLabs (GB/PL/PT), Autodesk (NO). Polling
    // it every week to discard everything it returns is worse than not polling.
    if (c.tier >= 4 && c.route === "ats" && c.status === "verified" && !c.countries?.length) {
      throw new RegistryError(
        `${c.name}: tier ${c.tier} companies need an explicit \`countries\` list before they can be verified - ` +
          `the default ${JSON.stringify(DEFAULT_DEFAULTS.allowed_countries)} would filter their whole board away`,
      )
    }
    // §8: a board is only `verified` on evidence at or above vendor_identifier.
    if (c.status === "verified" && c.route === "ats") {
      const kind = c.identity?.evidence_kind
      if (!kind || !PROMOTING_EVIDENCE.includes(kind as (typeof PROMOTING_EVIDENCE)[number])) {
        throw new RegistryError(
          `${c.name}: status verified needs identity.evidence_kind in {${PROMOTING_EVIDENCE.join(", ")}} - a working slug is not evidence`,
        )
      }
    }
    companies.push(c)
  }
  return { _comment: raw._comment, defaults, companies }
}

/** Merge registry defaults with a company's overrides into a filter config. */
export function filterConfigFor(company: Company, defaults: RegistryDefaults): FilterConfig {
  return {
    allowed_countries: company.countries?.length ? company.countries : defaults.allowed_countries,
    remote_regions: company.remote_regions?.length ? company.remote_regions : defaults.remote_regions,
    cities: company.cities?.length ? company.cities : defaults.cities,
    title_include: company.title_include?.length ? company.title_include : defaults.title_include,
    title_exclude: company.title_exclude?.length ? company.title_exclude : defaults.title_exclude,
    conditional_exclude: defaults.conditional_exclude,
  }
}

/**
 * Vendors whose published policy we have read and that said no. This lives in
 * code, not only in the registry, so the answer is **fail-closed**: a fresh
 * `companies.json` copied from the example, or one whose `vendor_access` block
 * was deleted, still does not hit a host that disallowed us. Turning one on is
 * an explicit `enabled: true` in the registry - a decision someone made, never a
 * default someone inherited.
 */
const POLICY_BLOCKED: Record<string, string> = {
  smartrecruiters:
    "api.smartrecruiters.com/robots.txt is `User-agent: LinkedInBot / Allow: /v1/companies/` then " +
    "`User-agent: * / Disallow: /` (checked 2026-08-18): every agent but LinkedIn's is disallowed " +
    "from the whole host. Set defaults.vendor_access.smartrecruiters.enabled to true only if you " +
    "have decided to run it as personal use, at low volume, at your own responsibility.",
}

/**
 * May this vendor be fetched at all? A vendor in POLICY_BLOCKED is off unless the
 * registry explicitly turns it on; every other vendor is on unless the registry
 * turns it off. The reason travels with the skip, so a silently missing company
 * is impossible.
 */
export function vendorAccess(defaults: RegistryDefaults, vendor: string): VendorAccess {
  const configured = defaults.vendor_access?.[vendor]
  const blocked = POLICY_BLOCKED[vendor]
  if (blocked) {
    if (configured?.enabled === true) return configured
    return { enabled: false, robots: "disallow", why: configured?.why ?? blocked }
  }
  return configured ?? { enabled: true }
}

/** Minimum days between successful fetches: per-company wins, else the tier default. */
export function cadenceFor(company: Company, defaults: RegistryDefaults): number {
  if (typeof company.cadence_days === "number") return company.cadence_days
  const byTier = defaults.cadence_days[String(company.tier)]
  return typeof byTier === "number" ? byTier : 7
}

export interface Selection {
  due: Company[]
  /** Companies that were eligible but did not fit under `maxCompanies`. */
  deferred: Company[]
  /** Why each ineligible company was not considered, for the run report. */
  skipped: Array<{ name: string; reason: string }>
}

/**
 * Tier-weighted staleness rotation (§2). This is what "weekly" means without a
 * calendar: order the due companies by how long it has been, break ties by tier,
 * and take the first `maxCompanies`. Companies that do not fit keep their old
 * `last_success_at`, so the next click picks them first.
 */
export function selectDue(
  registry: Registry,
  nowISO: string,
  maxCompanies: number,
): Selection {
  const skipped: Array<{ name: string; reason: string }> = []
  const eligible: Array<{ company: Company; staleness: number }> = []
  const nowMs = Date.parse(nowISO)

  for (const company of registry.companies) {
    if (company.route !== "ats") {
      skipped.push({ name: company.name, reason: `route ${company.route}` })
      continue
    }
    if (company.status !== "verified") {
      skipped.push({ name: company.name, reason: `status ${company.status}` })
      continue
    }
    if (!company.vendor || !company.token) {
      skipped.push({ name: company.name, reason: "no vendor/token" })
      continue
    }
    const access = vendorAccess(registry.defaults, company.vendor)
    if (!access.enabled) {
      skipped.push({
        name: company.name,
        reason: `vendor ${company.vendor} disabled in defaults.vendor_access${access.why ? ` - ${access.why}` : ""}`,
      })
      continue
    }
    const cadence = cadenceFor(company, registry.defaults)
    if (cadence <= 0) {
      skipped.push({ name: company.name, reason: "cadence_days 0 (manual only)" })
      continue
    }
    const last = company.last_success_at ? Date.parse(company.last_success_at) : NaN
    // Never fetched: infinitely stale, so a new company is always picked first.
    const days = isNaN(last) ? Number.POSITIVE_INFINITY : (nowMs - last) / 86400000
    if (days < cadence) {
      skipped.push({ name: company.name, reason: `not due (${days.toFixed(1)}d < ${cadence}d)` })
      continue
    }
    eligible.push({ company, staleness: days })
  }

  eligible.sort((a, b) => {
    if (a.staleness !== b.staleness) return b.staleness - a.staleness
    if (a.company.tier !== b.company.tier) return a.company.tier - b.company.tier
    return a.company.name.localeCompare(b.company.name)
  })

  return {
    due: eligible.slice(0, Math.max(0, maxCompanies)).map((e) => e.company),
    deferred: eligible.slice(Math.max(0, maxCompanies)).map((e) => e.company),
    skipped,
  }
}

/** A verified board that answered `not_found` this many times in a row is re-detected. */
export const MISSING_STREAK_LIMIT = 3
/** Days to wait after the 1st, 2nd and 3rd failed detection; after the 4th, ask. */
export const RESOLVE_BACKOFF_DAYS = [1, 3, 7]
/** An ATS with no adapter is looked at again monthly, in case one was added. */
export const UNSUPPORTED_RETRY_DAYS = 30

/**
 * The companies `resolve --due` works on, most deserving first: never-tried
 * rows, then the longest-waiting. Detection is retried with backoff and then
 * handed to the owner (`next_resolve_at: null`); `ambiguous` always waits for
 * the owner, because a wrong board means another company's jobs.
 */
export function selectResolveDue(registry: Registry, today: string, max: number): Company[] {
  const due = registry.companies.filter((c) => {
    if ((c.route ?? "ats") !== "ats") return false
    if (c.status === "verified") return (c.missing_streak ?? 0) >= MISSING_STREAK_LIMIT
    if (c.status !== "unresolved" && c.status !== "unsupported_vendor") return false
    if (c.next_resolve_at === null) return false
    return !c.next_resolve_at || c.next_resolve_at <= today
  })
  due.sort((a, b) => (a.last_resolve_at ?? "").localeCompare(b.last_resolve_at ?? "") || a.name.localeCompare(b.name))
  return due.slice(0, Math.max(0, max))
}

function addDays(today: string, days: number): string {
  const d = new Date(`${today}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

/** The retry fields one detection outcome leaves behind (COMPANIES_PLAN §3.2). */
export function resolveBookkeeping(company: Company, status: ResolutionStatus, today: string): Partial<Company> {
  if (status === "verified") {
    return { resolve_attempts: 0, last_resolve_at: today, next_resolve_at: undefined, missing_streak: 0 }
  }
  const attempts = (company.resolve_attempts ?? 0) + 1
  let next: string | null
  if (status === "unsupported_vendor") next = addDays(today, UNSUPPORTED_RETRY_DAYS)
  else if (status === "unresolved" && attempts <= RESOLVE_BACKOFF_DAYS.length) next = addDays(today, RESOLVE_BACKOFF_DAYS[attempts - 1])
  else next = null
  return { resolve_attempts: attempts, last_resolve_at: today, next_resolve_at: next }
}

export function emptyStats(): CompanyStats {
  return {
    jobs_seen: 0, german_gated: 0, eligible_jobs: 0, last_eligible_at: null,
    last_jobs_seen: null, last_eligible_jobs: null,
  }
}

export interface Bookkeeping {
  name: string
  attempted_at: string
  status: CompanyStatus | string
  /** Only a genuinely successful fetch moves `last_success_at` (§10.3). */
  success: boolean
  jobs_seen: number
  eligible: number
}

/**
 * Fold one run's bookkeeping back into the registry file. Read-modify-write on
 * the file as it is *now*, so a hand edit made while the fetch was in flight is
 * preserved; only the bookkeeping keys are touched.
 */
export function writeBookkeeping(entries: Bookkeeping[], path = registryPath()): void {
  if (!entries.length) return
  withRegistryLock(path, () => {
  const before = JSON.parse(readFileSync(path, "utf-8")) as Registry
  const raw = JSON.parse(JSON.stringify(before)) as Registry
  const byName = new Map(entries.map((e) => [e.name, e]))
  for (const company of raw.companies) {
    const entry = byName.get(company.name)
    if (!entry) continue
    company.last_attempt_at = entry.attempted_at
    company.last_status = entry.status
    if (entry.success) company.last_success_at = entry.attempted_at
    // Only a board that is *gone* counts towards re-detection; a rate limit or a
    // 5xx says nothing about whether the token is still right.
    if (entry.success) company.missing_streak = 0
    else if (entry.status === "not_found") company.missing_streak = (company.missing_streak ?? 0) + 1
    const stats: CompanyStats = { ...emptyStats(), ...(company.stats ?? {}) }
    stats.jobs_seen += entry.jobs_seen
    stats.eligible_jobs += entry.eligible
    stats.last_jobs_seen = entry.jobs_seen
    stats.last_eligible_jobs = entry.eligible
    if (entry.eligible > 0) stats.last_eligible_at = entry.attempted_at.slice(0, 10)
    company.stats = stats
  }
  writeJsonAtomic(path.replace(/\.json$/, ".backup.json"), before)
  writeJsonAtomic(path, raw)
  })
}

/**
 * Apply a resolution outcome to one company, in place, and persist it.
 *
 * The whole tool-owned resolution state moves together: a row promoted to
 * `verified` loses the candidate list that was only ever a question, and a row
 * that stops being verified loses the `identity` that no longer holds. Leaving
 * either behind produces a registry that contradicts itself - "verified, and
 * here are the boards we are still unsure about". Hand-owned fields are never
 * touched.
 */
export function writeResolution(
  updates: Array<{ name: string; patch: Partial<Company> }>,
  path = registryPath(),
): void {
  if (!updates.length) return
  withRegistryLock(path, () => {
  const before = JSON.parse(readFileSync(path, "utf-8")) as Registry
  const raw = JSON.parse(JSON.stringify(before)) as Registry
  const defaults: RegistryDefaults = { ...DEFAULT_DEFAULTS, ...(raw.defaults ?? {}) }
  const byName = new Map(updates.map((u) => [u.name, u.patch]))
  for (const company of raw.companies) {
    const patch = byName.get(company.name)
    if (!patch) continue
    Object.assign(company, patch)
    // `undefined` in a patch means "remove the field", which JSON would
    // otherwise keep as a stale value from the previous state.
    for (const [key, value] of Object.entries(patch)) {
      if (value === undefined) delete (company as unknown as Record<string, unknown>)[key]
    }
    if (company.status === "verified") {
      delete company.candidates
      // Phase 0 is only "done" when every route:ats company carries an explicit
      // cadence (§16), so promotion writes the effective one down instead of
      // leaving it implied by the tier.
      if (company.route === "ats" && typeof company.cadence_days !== "number") {
        company.cadence_days = cadenceFor(company, defaults)
      }
    } else if (patch.identity === undefined) {
      // No longer verified: the old evidence describes a board we are no longer
      // claiming, so it goes with the status.
      company.identity = null
    }
  }
  writeJsonAtomic(path.replace(/\.json$/, ".backup.json"), before)
  writeJsonAtomic(path, raw)
  })
}
