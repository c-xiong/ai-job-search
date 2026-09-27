// Transport, status classification, ids, sorting, and the small text utilities
// every adapter needs. No vendor knowledge lives here.

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync, appendFileSync } from "fs"
import { dirname, join } from "path"
import { VENDORS, type CompanyStatus, type JobResult, type VendorName } from "./types.ts"

/** Repo root: src -> cli -> ats-search -> skills -> .agents -> root. */
export const ROOT = join(import.meta.dir, "..", "..", "..", "..", "..")

/** An honest UA that names the tool, per the portal-skill contract. */
export const UA = "ats-search-cli/1.0"

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
}

/** Thrown by a vendor parser when a 200 does not parse into the expected shape. */
export class SchemaError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "SchemaError"
  }
}

/** Thrown when the registry is missing, unparseable, or internally invalid. */
export class RegistryError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "RegistryError"
  }
}

export interface HttpResponse {
  status: number
  body: string
  /** The URL the response actually came from, after redirects. */
  url: string
}

export interface FetchOpts {
  accept?: string
  /** Follow redirects. Board endpoints deliberately do NOT: Personio answers a
   *  non-customer subdomain with a 307, and following it would turn a missing
   *  board into a confusing 200. Careers pages, which redirect constantly, do. */
  follow?: boolean
  /** GET unless said otherwise. Workday's listing is a JSON POST. */
  method?: "GET" | "POST"
  /** A JSON request body, sent as-is with Content-Type application/json. */
  body?: string
}

export type Transport = (url: string, opts?: FetchOpts) => Promise<HttpResponse>

/**
 * Map an HTTP outcome onto the per-company status vocabulary. Personio answers a
 * non-customer subdomain with 307, not 404, so a redirect that leaves the board
 * host is a missing board rather than a success.
 */
export function classifyStatus(status: number): CompanyStatus | "success" {
  // The transport reports a connection failure, DNS failure or exhausted retry
  // budget as 0. Falling through to "success" here handed the error text to a
  // parser, which then reported `schema_changed` - a loud "the vendor changed
  // their API" alert for what was actually a flaky network.
  if (status <= 0) return "unavailable"
  if (status === 429) return "rate_limited"
  if (status === 404 || status === 410 || (status >= 300 && status < 400)) return "not_found"
  if (status >= 500) return "unavailable"
  if (status >= 400) return "not_found"
  return "success"
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

// Per-host serialization plus a 250 ms gap: five vendors, ~8 boards a click, and
// no reason for any of it to arrive as a burst.
const HOST_QUEUE = new Map<string, Promise<unknown>>()
const SPACING_MS = 250

function serialize<T>(host: string, task: () => Promise<T>): Promise<T> {
  const prev = HOST_QUEUE.get(host) ?? Promise.resolve()
  const next = prev.then(async () => {
    const out = await task()
    await sleep(SPACING_MS)
    return out
  })
  // Keep the chain alive even when a task rejects, or one failure would wedge the host.
  HOST_QUEUE.set(host, next.catch(() => undefined))
  return next
}

/** Where cached board payloads and the rate-limit ledger live. */
export function cacheDir(): string {
  return process.env.ATS_CACHE_DIR || join(ROOT, "job_scraper", "ats_cache")
}

/**
 * "Any host returns 429 during a fetch -> stop that host for the day, with no
 * retry" (§18). Not just for this run: a 429 answered by a retry loop, or by the
 * next click ten minutes later, is the same discourtesy repeated. The ledger is
 * a small file next to the payload cache, so the stop survives the process.
 */
export interface HostGate {
  blockedUntil(host: string): string | null
  block(host: string): void
}

function endOfDayISO(): string {
  const d = now()
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1)).toISOString()
}

export function hostGate(): HostGate {
  const path = join(cacheDir(), "rate_limited.json")
  const ledger: Record<string, string> = readJson<Record<string, string>>(path) ?? {}
  return {
    blockedUntil(host) {
      const until = ledger[host]
      if (!until) return null
      if (Date.parse(until) <= now().getTime()) {
        delete ledger[host]
        return null
      }
      return until
    },
    block(host) {
      ledger[host] = endOfDayISO()
      try {
        writeJsonAtomic(path, ledger)
      } catch {
        // A ledger we cannot persist still holds for this process; losing it is
        // not a reason to fail the run.
      }
    },
  }
}

/**
 * The real transport: one GET, exponential backoff with jitter on 5xx, and a
 * hard timeout. A connection failure is reported as 0, which classifies as
 * `unavailable` rather than pretending the board is empty.
 *
 * A 429 is **never retried**. Backing off and asking again is what the response
 * asked us not to do; it comes straight back to the caller, which classifies it
 * `rate_limited` - a status that is never evidence of breakage.
 */
export function httpTransport(timeoutMs = 20000, maxRetries = 4): Transport {
  return async (url, opts = {}) => {
    const accept = opts.accept ?? "application/json"
    const host = new URL(url).host
    return serialize(host, async () => {
      let delay = 700
      for (let attempt = 0; ; attempt++) {
        let res: Response
        try {
          res = await fetch(url, {
            method: opts.method ?? "GET",
            headers: {
              "User-Agent": UA,
              Accept: accept,
              ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
            },
            ...(opts.body !== undefined ? { body: opts.body } : {}),
            redirect: opts.follow ? "follow" : "manual",
            signal: AbortSignal.timeout(timeoutMs),
          })
        } catch (e) {
          if (attempt >= maxRetries) {
            return { status: 0, body: e instanceof Error ? e.message : String(e), url }
          }
          await sleep(delay + Math.floor(Math.random() * 400))
          delay = Math.min(delay * 2, 8000)
          continue
        }
        if (res.status === 429) return { status: 429, body: "", url: res.url || url }
        if (res.status >= 500) {
          if (attempt >= maxRetries) return { status: res.status, body: "", url: res.url || url }
          await sleep(delay + Math.floor(Math.random() * 400))
          delay = Math.min(delay * 2, 8000)
          continue
        }
        const body = await res.text().catch(() => "")
        return { status: res.status, body, url: res.url || url }
      }
    })
  }
}

/**
 * A transport that answers from a fixture map instead of the network, selected by
 * the ATS_FIXTURES env var. This is how the offline tests exercise the whole CLI -
 * exit codes, partial failure, pagination - without a single request. The map is
 * `{ "<exact url>": { "file": "...", "status": 200 } }` next to the fixtures. A
 * request with a body is looked up as `"<url> <body>"` first, so paginated POSTs
 * can answer differently per page, then by the bare URL.
 */
export function fixtureTransport(dir: string): Transport {
  const map = JSON.parse(readFileSync(join(dir, "map.json"), "utf-8")) as Record<
    string,
    { file?: string; status?: number; body?: string }
  >
  return async (url, opts = {}) => {
    logRequest(url)
    const hit = (opts.body !== undefined ? map[`${url} ${opts.body}`] : undefined) ?? map[url]
    if (!hit) return { status: 404, body: "", url }
    const body = hit.body ?? (hit.file ? readFileSync(join(dir, hit.file), "utf-8") : "")
    return { status: hit.status ?? 200, body, url }
  }
}

/** Append every requested URL to ATS_REQUEST_LOG, so a test can count requests. */
function logRequest(url: string): void {
  const path = process.env.ATS_REQUEST_LOG
  if (path) appendFileSync(path, url + "\n", "utf-8")
}

/**
 * The transport a command should use: fixtures under test, the network
 * otherwise, and in both cases behind the rate-limit gate - a host that answered
 * 429 gets no further request today, from any command, without the caller
 * needing to remember.
 */
export function transport(): Transport {
  const dir = process.env.ATS_FIXTURES
  const inner = dir ? fixtureTransport(dir) : httpTransport()
  const gate = hostGate()
  return async (url, opts) => {
    let host: string
    try {
      host = new URL(url).host
    } catch {
      host = ""
    }
    const until = host ? gate.blockedUntil(host) : null
    if (until) {
      return { status: 429, body: `${host} answered 429 earlier; stopped until ${until}`, url }
    }
    if (!dir) logRequest(url)
    const res = await inner(url, opts)
    if (res.status === 429 && host) gate.block(host)
    return res
  }
}

/** `now`, overridable with ATS_NOW so cadence and recency tests are deterministic. */
export function now(): Date {
  const override = process.env.ATS_NOW
  return override ? new Date(override) : new Date()
}

export function todayISO(): string {
  return now().toISOString().slice(0, 10)
}

// ---------------------------------------------------------------------------
// Composite ids (§10.2)
// ---------------------------------------------------------------------------

export function compositeId(vendor: VendorName, token: string, postingId: string): string {
  return `${vendor}:${token}:${postingId}`
}

export interface ParsedId {
  vendor: VendorName
  token: string
  postingId: string
}

/**
 * Parse a composite id. A bare posting id is *not* accepted: across ~55 companies
 * and five vendors it is not unique, and guessing which board it belongs to is
 * how a detail lookup silently returns another company's job.
 */
export function parseCompositeId(input: string): ParsedId | null {
  const parts = input.split(":")
  if (parts.length < 3) return null
  const [vendor, token, ...rest] = parts
  if (!VENDORS.includes(vendor as VendorName)) return null
  const postingId = rest.join(":")
  if (!token || !postingId) return null
  return { vendor: vendor as VendorName, token, postingId }
}

// ---------------------------------------------------------------------------
// Sorting (§10.4)
// ---------------------------------------------------------------------------

/**
 * `prefit` desc -> `date` desc (nulls last) -> composite id asc. Total and
 * deterministic, so `--limit` truncates reproducibly and two runs over the same
 * payload produce byte-identical output.
 */
export function stableSort(results: JobResult[]): JobResult[] {
  return [...results].sort((a, b) => {
    if (b.prefit_score !== a.prefit_score) return b.prefit_score - a.prefit_score
    const ad = a.date ?? ""
    const bd = b.date ?? ""
    if (ad !== bd) {
      if (!ad) return 1
      if (!bd) return -1
      return ad < bd ? 1 : -1
    }
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
  })
}

// ---------------------------------------------------------------------------
// Text
// ---------------------------------------------------------------------------

function numericEntity(cp: number): string {
  return cp >= 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : ""
}

export function decodeEntities(text: string): string {
  return text
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/&#(\d+);/g, (_, dec) => numericEntity(parseInt(dec, 10)))
    .replace(/&#[xX]([0-9a-fA-F]+);/g, (_, hex) => numericEntity(parseInt(hex, 16)))
    // Last: an &amp;lt; in a double-escaped Greenhouse body must become &lt;, not <.
    .replace(/&amp;/g, "&")
}

/** HTML (or XHTML inside CDATA) to readable prose. Block tags become newlines. */
export function stripHtml(html: string | null | undefined): string | null {
  if (!html) return null
  const withBreaks = html
    .replace(/<\s*br\s*\/?>/gi, "\n")
    .replace(/<\/(p|li|ul|ol|div|h\d|tr)>/gi, "\n")
  const text = decodeEntities(withBreaks.replace(/<[^>]+>/g, " "))
    .replace(/[ \t]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
  return text || null
}

/**
 * Normalize a vendor date to ISO `YYYY-MM-DD`. Accepts ISO strings and epoch
 * milliseconds (Lever). Anything unparseable returns null - a missing date is
 * reported as null and the posting is kept (§9 step 5); it is never `now()`.
 */
export function isoDate(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null
  if (typeof value === "number") {
    if (!Number.isFinite(value) || value <= 0) return null
    const d = new Date(value)
    return isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10)
  }
  if (typeof value !== "string") return null
  const trimmed = value.trim()
  if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) return trimmed
  if (/^\d{10,}$/.test(trimmed)) return isoDate(parseInt(trimmed, 10))
  const d = new Date(trimmed)
  return isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10)
}

/** Whole days between two ISO dates (b - a). */
export function daysBetween(a: string, b: string): number {
  const ms = Date.parse(b) - Date.parse(a)
  return Math.floor(ms / 86400000)
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------

/**
 * Write JSON through a temp file in the same directory and rename it into place,
 * so an interrupted run can never leave a half-written registry or cache entry.
 */
export function writeJsonAtomic(path: string, data: unknown): void {
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp-${process.pid}`
  writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf-8")
  renameSync(tmp, path)
}

export function readJson<T>(path: string): T | null {
  if (!existsSync(path)) return null
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as T
  } catch {
    return null
  }
}
