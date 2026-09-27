// Workday "cxs" job-site API. Keyless: it is what the public careers site itself
// calls. The listing is a JSON POST, 20 postings per page, newest first; it
// carries title, path and a location summary but no description and no absolute
// date ("Posted 3 Days Ago"), so `date` stays null - the repo never synthesizes
// one - and descriptions arrive later from the posting page, as for SmartRecruiters.
//
// The token is "<tenant>.<pod>/<site>" (novartis.wd3/Novartis_Careers): all three
// parts are needed and none can be derived from a company name, so Workday is
// found only through a link on the careers page or a pasted board URL.
//
// Big tenants publish 800-2,000 postings. When the first page offers a country
// facet (Novartis: `locationCountry`) the board is re-queried with the owner's
// countries applied; otherwise it pages newest-first up to MAX_PAGES and says so.
//
// Verified live 2026-09-28 against Novartis, Roche and NVIDIA. robots.txt on those
// tenants allows the site paths and disallows only /refreshFacet/, which is unused.

import { SchemaError, classifyStatus } from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import type { BoardResult, RawPosting } from "../types.ts"
import { hints, type BoardHint, type Vendor } from "./vendor.ts"

const PAGE = 20
const MAX_PAGES = 25

/** Country names Workday shows in a country facet, for the codes the owner uses. */
const COUNTRY_NAMES: Record<string, string[]> = {
  CH: ["switzerland", "schweiz", "suisse"],
  DE: ["germany", "deutschland"],
  AT: ["austria", "österreich", "osterreich"],
}

interface Parts { tenant: string; pod: string; site: string }

export function parseToken(token: string): Parts | null {
  const m = token.match(/^([a-z0-9-]+)\.(wd\d+)\/([A-Za-z0-9_-]+)$/i)
  return m ? { tenant: m[1].toLowerCase(), pod: m[2].toLowerCase(), site: m[3] } : null
}

const host = (p: Parts) => `https://${p.tenant}.${p.pod}.myworkdayjobs.com`

interface WorkdayFacetValue { descriptor?: string; id?: string; facetParameter?: string; values?: WorkdayFacetValue[] }
interface WorkdayFacet { facetParameter?: string; descriptor?: string; values?: WorkdayFacetValue[] }
interface WorkdayPosting { title?: string; externalPath?: string; locationsText?: string; bulletFields?: string[] }
interface WorkdayPage { total?: number; jobPostings?: WorkdayPosting[]; facets?: WorkdayFacet[] }

export function parsePage(body: string): WorkdayPage {
  let data: WorkdayPage
  try {
    data = JSON.parse(body)
  } catch {
    throw new SchemaError("workday: response is not JSON")
  }
  if (!data || !Array.isArray(data.jobPostings)) {
    throw new SchemaError("workday: payload has no `jobPostings` array")
  }
  return data
}

/** Facet ids for the wanted countries, from any facet that is a country list.
 *  Facets nest (`locationMainGroup` > `locationCountry`), so this walks them. */
export function countryFacet(facets: WorkdayFacet[] | undefined, countries: string[]): Record<string, string[]> {
  const wanted = countries.flatMap((c) => COUNTRY_NAMES[c.toUpperCase()] ?? [])
  const out: Record<string, string[]> = {}
  const walk = (list: WorkdayFacet[] | WorkdayFacetValue[] | undefined) => {
    for (const facet of list ?? []) {
      const param = facet.facetParameter ?? ""
      const values = facet.values ?? []
      if (/country/i.test(param) || /country/i.test(facet.descriptor ?? "")) {
        const ids = values
          .filter((v) => v.id && wanted.includes((v.descriptor ?? "").trim().toLowerCase()))
          .map((v) => v.id as string)
        if (ids.length) out[param] = ids
      }
      walk(values.filter((v) => v.facetParameter))
    }
  }
  walk(facets)
  return out
}

export function toPostings(page: WorkdayPage, parts: Parts): RawPosting[] {
  const postings: RawPosting[] = []
  for (const raw of page.jobPostings ?? []) {
    const path = (raw.externalPath ?? "").trim()
    if (!path) continue
    const id = (raw.bulletFields?.[0] ?? "").trim() || path.replace(/^\//, "")
    const where = (raw.locationsText ?? "").trim()
    postings.push({
      posting_id: id,
      title: (raw.title ?? "").trim() || "(untitled)",
      company: null,
      // "3 Locations" is a count, not a place: better unknown than a fake city.
      locations: hints([[/^\d+ locations?$/i.test(where) ? "" : where, null]]),
      workplace: null,
      date: null,
      updated_at: null,
      url: `${host(parts)}/${parts.site}${path}`,
      description: null,
      deadline: null,
      listed: true,
    })
  }
  return postings
}

async function page(parts: Parts, get: Transport, offset: number, facets: Record<string, string[]>) {
  const url = `${host(parts)}/wday/cxs/${parts.tenant}/${parts.site}/jobs`
  const body = JSON.stringify({ appliedFacets: facets, limit: PAGE, offset, searchText: "" })
  return get(url, { method: "POST", body })
}

export const workday: Vendor = {
  name: "workday",
  hasVendorIdentity: false,
  inlineDescriptions: false,
  guessable: false,
  boardUrl: (token) => {
    const p = parseToken(token)
    return p ? `${host(p)}/${p.site}` : ""
  },
  endpoint: (token) => {
    const p = parseToken(token)
    return p ? `${host(p)}/wday/cxs/${p.tenant}/${p.site}/jobs` : "https://invalid.myworkdayjobs.com/"
  },
  detectToken: (html) => {
    const jobs = html.match(/([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com\/(?:[a-z]{2}-[A-Z]{2}\/)?([A-Za-z0-9_-]+)/i)
    if (jobs && !/^(wday|job|refreshFacet)$/i.test(jobs[3])) return `${jobs[1].toLowerCase()}.${jobs[2].toLowerCase()}/${jobs[3]}`
    const site = html.match(/(wd\d+)\.myworkdaysite\.com\/(?:[a-z]{2}-[A-Z]{2}\/)?recruiting\/([a-z0-9-]+)\/([A-Za-z0-9_-]+)/i)
    if (site) return `${site[2].toLowerCase()}.${site[1].toLowerCase()}/${site[3]}`
    return null
  },

  async fetchBoard(token, get, hint?: BoardHint): Promise<BoardResult> {
    const parts = parseToken(token)
    if (!parts) {
      return { status: "not_found", postings: [], identity: null, message: `workday token "${token}" is not tenant.pod/site`, requests: 0 }
    }
    let requests = 0
    let facets: Record<string, string[]> = {}
    let res = await page(parts, get, 0, facets)
    requests++
    let cls = classifyStatus(res.status)
    // Workday answers an unknown tenant or site with 422 rather than 404.
    if (res.status === 422) cls = "not_found"
    if (cls !== "success") {
      return { status: cls, postings: [], identity: null, message: `HTTP ${res.status}`, requests }
    }
    try {
      let first = parsePage(res.body)
      const narrowed = hint?.countries?.length ? countryFacet(first.facets, hint.countries) : {}
      let scope = ""
      if (Object.keys(narrowed).length) {
        facets = narrowed
        res = await page(parts, get, 0, facets)
        requests++
        cls = classifyStatus(res.status)
        if (cls !== "success") {
          return { status: cls, postings: [], identity: null, message: `HTTP ${res.status} (country filter)`, requests }
        }
        first = parsePage(res.body)
        scope = ` in ${hint!.countries!.join("/")}`
      }
      // Later pages may report total 0; the first page's count is the real one.
      const total = first.total ?? 0
      const postings = toPostings(first, parts)
      let offset = PAGE
      let pages = 1
      while (offset < total && pages < MAX_PAGES) {
        const next = await page(parts, get, offset, facets)
        requests++
        if (classifyStatus(next.status) !== "success") {
          return {
            status: "success_partial", postings, identity: null, requests,
            message: `stopped at ${postings.length} of ${total}${scope}: HTTP ${next.status}`,
          }
        }
        const more = toPostings(parsePage(next.body), parts)
        if (!more.length) break
        postings.push(...more)
        offset += PAGE
        pages++
      }
      const capped = offset < total
      return {
        status: postings.length ? "ok" : "empty",
        postings,
        identity: null,
        message: capped ? `newest ${postings.length} of ${total} postings${scope}` : null,
        requests,
      }
    } catch (e) {
      return { status: "schema_changed", postings: [], identity: null, message: e instanceof Error ? e.message : String(e), requests }
    }
  },

  // The listing is the only index; a detail lookup finds the posting there.
  async fetchDetail(token, postingId, get) {
    const board = await workday.fetchBoard(token, get)
    return board.postings.find((p) => p.posting_id === postingId) ?? null
  },
}
