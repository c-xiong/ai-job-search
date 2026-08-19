// `resolve` - find a company's board, and decide honestly whether we know it is
// *theirs* (§8).
//
// Evidence-first, then slug fallback (§16). A blind slug sweep spends ~6 requests
// per company to produce the weakest possible evidence - "a token that happens to
// answer 200" - and the Personio probe that started this plan is the proof: the
// obvious slug `unique` resolves to a board belonging to "unique land use GmbH",
// not to the Zurich AI company. Reading the company's own careers page first costs
// fewer requests and produces the strongest evidence in the hierarchy.

import { classifyStatus, now, transport, writeError, RegistryError } from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import { ADAPTERS, adapterFor } from "../vendors/index.ts"
import {
  loadRegistry,
  vendorAccess,
  writeResolution,
  type Company,
  type RegistryDefaults,
  type ResolutionStatus,
} from "../registry.ts"
import { VENDORS, type VendorName } from "../types.ts"

/** ATS platforms we can recognize on a careers page but have no adapter for. */
const UNSUPPORTED_ATS: Array<[string, RegExp]> = [
  ["workday", /myworkdayjobs\.com|workday\.com\/[a-z-]+\/jobs/i],
  ["successfactors", /successfactors\.(?:eu|com)|jobs\.sap\.com/i],
  ["recruitee", /([a-z0-9-]+)\.recruitee\.com/i],
  ["join.com", /join\.com\/companies\//i],
  ["softgarden", /softgarden\.io|softgarden\.de/i],
  ["teamtailor", /[a-z0-9-]+\.teamtailor\.com/i],
  ["workable", /apply\.workable\.com/i],
  ["bamboohr", /[a-z0-9-]+\.bamboohr\.com/i],
  ["taleo", /taleo\.net/i],
  ["icims", /icims\.com/i],
]

export interface ResolveOpts {
  only: string[]
  allUnresolved: boolean
  maxCompanies: number
  /** Slug-fallback probes per company, after the careers page found nothing. */
  maxProbes: number
  dryRun: boolean
  format: "json" | "table"
}

export interface ResolveReport {
  name: string
  status: ResolutionStatus
  vendor: VendorName | null
  token: string | null
  evidence_kind: string | null
  evidence: string | null
  candidates: Array<{ vendor: string; token: string; note?: string }>
  requests: number
  detail: string
}

function normalize(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "")
}

const LEGAL_SUFFIX = /\s+(ag|gmbh|sa|sarl|inc|ltd|limited|llc|bv|nv|oy|ab|plc|se|holding|group)\.?$/i

/** Deterministic slug candidates, best-guess first. The §0 probe reached 17/18
 *  companies with exactly this kind of list - including the non-obvious
 *  `sonarsource` - which is why it is worth keeping as a *fallback*. */
export function slugCandidates(company: Company): string[] {
  const out: string[] = []
  const push = (value: string | null | undefined) => {
    const v = (value ?? "").trim().toLowerCase()
    if (v && /^[a-z0-9][a-z0-9-]*$/.test(v) && !out.includes(v)) out.push(v)
  }
  const domain = (company.domain ?? "").replace(/^https?:\/\//, "").replace(/^www\./, "")
  const label = domain.split(".")[0] ?? ""
  push(label)
  push(label.replace(/-/g, ""))
  const bare = company.name.replace(LEGAL_SUFFIX, "")
  push(normalize(bare))
  push(bare.trim().toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, ""))
  push(normalize(bare.split(/\s+/)[0] ?? ""))
  for (const alias of company.aliases ?? []) push(normalize(alias.replace(LEGAL_SUFFIX, "")))
  return out
}

/**
 * Does a vendor-supplied identifier name this company?
 *
 * Exact match after stripping a legal suffix and normalizing - deliberately not
 * a prefix match. "unique land use GmbH" starts with "unique", and a prefix rule
 * would therefore have promoted the Freiburg land-use company's Personio board
 * as Unique AG's. Brand and legal variants belong in `aliases`, where they are
 * a decision you made rather than a coincidence of spelling.
 */
export function identityMatches(company: Company, identity: string | null): boolean {
  if (!identity) return false
  const got = normalize(identity.replace(LEGAL_SUFFIX, ""))
  if (!got) return false
  const wanted = [company.name, ...(company.aliases ?? [])].map((n) => normalize(n.replace(LEGAL_SUFFIX, "")))
  return wanted.some((w) => w !== "" && w === got)
}

async function careersHtml(company: Company, get: Transport): Promise<{ html: string; pages: string[]; requests: number }> {
  const domain = (company.domain ?? "").replace(/^https?:\/\//, "").replace(/\/+$/, "")
  if (!domain) return { html: "", pages: [], requests: 0 }
  const urls = [`https://${domain}/careers`, `https://${domain}/jobs`, `https://${domain}`]
  let html = ""
  const pages: string[] = []
  let requests = 0
  for (const url of urls) {
    const res = await get(url, { accept: "text/html", follow: true })
    requests++
    if (classifyStatus(res.status) !== "success") continue
    html += "\n" + res.body
    pages.push(res.url)
    // Stop as soon as a supported board is named: one page is usually enough.
    if (VENDORS.some((v) => ADAPTERS[v].detectToken(html))) break
  }
  return { html, pages, requests }
}

async function resolveOne(
  company: Company,
  opts: ResolveOpts,
  get: Transport,
  blockedHosts: Set<string>,
  defaults: RegistryDefaults,
): Promise<ResolveReport> {
  const report: ResolveReport = {
    name: company.name,
    status: "unresolved",
    vendor: null,
    token: null,
    evidence_kind: null,
    evidence: null,
    candidates: [],
    requests: 0,
    detail: "",
  }
  if (!company.domain) {
    report.detail = "no `domain` in the registry - resolve cannot look at a careers page without one"
    return report
  }

  // 1-3. Read the company's own careers page, identify the ATS from the markup,
  //      then probe only that vendor.
  const { html, pages, requests } = await careersHtml(company, get)
  report.requests += requests
  for (const vendor of VENDORS) {
    const token = ADAPTERS[vendor].detectToken(html)
    if (!token) continue
    if (!vendorAccess(defaults, vendor).enabled) {
      report.status = "unsupported_vendor"
      report.vendor = vendor
      report.token = token
      report.detail = `careers page names ${vendor}:${token}, but that vendor is disabled in defaults.vendor_access - enable it there first if you decide to`
      return report
    }
    const host = new URL(ADAPTERS[vendor].endpoint(token)).host
    if (blockedHosts.has(host)) {
      report.detail = `${vendor} named on ${pages[0] ?? company.domain} but ${host} is rate-limited; stopped for today`
      report.candidates.push({ vendor, token, note: "not probed (host rate-limited)" })
      report.status = "ambiguous"
      return report
    }
    const board = await ADAPTERS[vendor].fetchBoard(token, get)
    report.requests += board.requests
    if (board.status === "rate_limited") blockedHosts.add(host)
    report.vendor = vendor
    report.token = token
    if (board.status === "ok" || board.status === "empty") {
      report.status = "verified"
      report.evidence_kind = "company_site_link"
      report.evidence = `${pages[0] ?? company.domain} links to ${ADAPTERS[vendor].boardUrl(token)}`
      report.detail = `${board.postings.length} postings`
      // A vendor identifier that contradicts the site link is a real conflict,
      // not a detail: something is wrong with either the link or the registry name.
      if (board.identity && !identityMatches(company, board.identity)) {
        report.status = "ambiguous"
        report.evidence_kind = null
        report.detail = `careers page links to ${vendor}:${token}, but that board says it belongs to "${board.identity}"`
        report.candidates.push({ vendor, token, note: `board identity "${board.identity}"` })
      }
      return report
    }
    report.status = "ambiguous"
    report.detail = `careers page names ${vendor}:${token} but the board answered ${board.status}`
    report.candidates.push({ vendor, token, note: board.status })
    return report
  }

  // A recognizable ATS we have no adapter for: that is a known answer, not silence.
  for (const [platform, re] of UNSUPPORTED_ATS) {
    if (re.test(html)) {
      report.status = "unsupported_vendor"
      report.detail = `careers page uses ${platform}, which v1 has no adapter for - route: linkedin meanwhile`
      report.evidence_kind = "company_site_link"
      report.evidence = `${pages[0] ?? company.domain} embeds ${platform}`
      return report
    }
  }

  // 4. Slug fallback, only for what is still unresolved. A hit here is `ambiguous`
  //    until identity evidence exists - never `verified` (§16 step 4).
  const candidates = slugCandidates(company)
  let probes = 0
  for (const token of candidates) {
    for (const vendor of VENDORS) {
      if (probes >= opts.maxProbes) break
      if (!vendorAccess(defaults, vendor).enabled) continue
      const host = new URL(ADAPTERS[vendor].endpoint(token)).host
      if (blockedHosts.has(host)) continue
      probes++
      const board = await ADAPTERS[vendor].fetchBoard(token, get)
      report.requests += board.requests
      if (board.status === "rate_limited") {
        blockedHosts.add(host)
        continue
      }
      if (board.status !== "ok" && board.status !== "empty") continue
      if (identityMatches(company, board.identity)) {
        report.status = "verified"
        report.vendor = vendor
        report.token = token
        report.evidence_kind = "vendor_identifier"
        report.evidence = `${vendor} board "${token}" reports company "${board.identity}"`
        report.detail = `${board.postings.length} postings`
        return report
      }
      report.candidates.push({
        vendor,
        token,
        note: board.identity ? `board identity "${board.identity}" does not match` : `${board.postings.length} postings, no company identifier in the payload`,
      })
    }
    if (probes >= opts.maxProbes) break
  }

  if (report.candidates.length) {
    report.status = "ambiguous"
    report.detail =
      `slug guesses found ${report.candidates.length} candidate board(s) with no identity evidence - ` +
      `confirm by hand, then set status verified with evidence_kind human_confirmed`
    return report
  }
  // "All my guesses failed" is never `no_public_board`: a custom domain, an
  // uncovered ATS, a non-derivable token, an empty board and a JS-rendered
  // careers page all look identical from out here (§8).
  report.status = "unresolved"
  report.detail = `no board found on the careers page and ${probes} slug probes; a human still has to look before this is no_public_board`
  return report
}

export async function runResolve(opts: ResolveOpts, get: Transport = transport()): Promise<number> {
  let registry
  try {
    registry = loadRegistry()
  } catch (e) {
    if (e instanceof RegistryError) {
      writeError(e.message, "BAD_REGISTRY")
      return 1
    }
    throw e
  }

  let targets: Company[]
  if (opts.only.length) {
    // Every selector is resolved and every miss is reported. Resolving three of
    // four companies and exiting 0 reads as "all done" - and the one you
    // mistyped is the one you then believe you have checked.
    const picked = new Map<string, Company>()
    const missing: string[] = []
    for (const selector of opts.only) {
      const wanted = selector.trim().toLowerCase()
      const matches = registry.companies.filter(
        (c) =>
          c.name.toLowerCase() === wanted ||
          (c.aliases ?? []).some((a) => a.toLowerCase() === wanted) ||
          (c.token ?? "").toLowerCase() === wanted,
      )
      if (!matches.length) {
        missing.push(selector)
        continue
      }
      for (const match of matches) picked.set(match.name, match)
    }
    if (missing.length) {
      writeError(`no registry entry matches: ${missing.join(", ")}`, "BAD_ARGS")
      return 1
    }
    targets = [...picked.values()]
  } else if (opts.allUnresolved) {
    targets = registry.companies.filter((c) => c.status === "unresolved" && c.route !== "manual")
  } else {
    writeError("resolve needs --company <name> (repeatable) or --all-unresolved", "BAD_ARGS")
    return 1
  }
  targets = targets.slice(0, Math.max(0, opts.maxCompanies))

  const blockedHosts = new Set<string>()
  const reports: ResolveReport[] = []
  for (const company of targets) {
    reports.push(await resolveOne(company, opts, get, blockedHosts, registry.defaults))
  }

  if (!opts.dryRun) {
    const stamp = now().toISOString().slice(0, 10)
    writeResolution(
      reports.map((r) => ({
        name: r.name,
        patch: {
          status: r.status,
          ...(r.vendor ? { vendor: r.vendor } : {}),
          ...(r.token ? { token: r.token } : {}),
          ...(r.status === "verified" && r.evidence_kind
            ? { identity: { evidence_kind: r.evidence_kind, evidence: r.evidence ?? "", verified_at: stamp } }
            : {}),
          ...(r.candidates.length ? { candidates: r.candidates } : {}),
          // Nothing here promotes a route on its own: a board that resolved still
          // needs you to decide it is worth polling.
        } as Partial<Company>,
      })),
    )
  }

  const counts: Record<string, number> = {}
  for (const r of reports) counts[r.status] = (counts[r.status] ?? 0) + 1
  if (opts.format === "json") {
    process.stdout.write(
      JSON.stringify(
        { meta: { resolved: reports.length, status_counts: counts, dry_run: opts.dryRun }, results: reports },
        null,
        2,
      ) + "\n",
    )
  } else {
    for (const r of reports) {
      process.stdout.write(
        `${r.status.padEnd(18)} ${r.name}${r.vendor ? `  -> ${r.vendor}:${r.token}` : ""}\n` +
          `${" ".repeat(19)}${r.detail}\n` +
          (r.evidence ? `${" ".repeat(19)}evidence: ${r.evidence}\n` : "") +
          r.candidates.map((c) => `${" ".repeat(19)}candidate ${c.vendor}:${c.token} - ${c.note ?? ""}\n`).join(""),
      )
    }
    process.stdout.write(
      `\n${reports.length} companies: ` +
        Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(", ") +
        (opts.dryRun ? "  (dry run - registry not written)" : "") +
        "\n",
    )
  }
  return 0
}
