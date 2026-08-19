// SmartRecruiters postings API. The only v1 vendor that paginates, the only one
// without inline descriptions, and the one with the false-200 trap:
//
//     GET /v1/companies/<any garbage slug>/postings
//     200 {"offset":0,"limit":2,"totalFound":0,"content":[]}
//
// confirmed live 2026-08-18 against a nonsense slug. A wrong guess is therefore
// indistinguishable from an empty board, so a zero-result board is classified
// `not_found`, never `empty` - the conservative direction, because `empty` would
// keep a mis-resolved company quietly in rotation forever.

import { SchemaError, classifyStatus, isoDate, stripHtml } from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import type { BoardResult, RawPosting } from "../types.ts"
import { firstMatch, hints, type Vendor } from "./vendor.ts"

const API = "https://api.smartrecruiters.com/v1/companies"
const MAX_PAGES = 20

// 100 is the vendor's own maximum. ATS_SR_PAGE_SIZE exists so the pagination path
// can be exercised against a 3-posting fixture instead of a 101-posting one, and
// it is read per call rather than at import time so a test can set it.
function pageSize(): number {
  return Math.max(1, Number(process.env.ATS_SR_PAGE_SIZE ?? 100) || 100)
}

interface SrPosting {
  id?: string
  uuid?: string
  name?: string
  releasedDate?: string
  company?: { identifier?: string; name?: string }
  location?: { city?: string; region?: string; country?: string; remote?: boolean; hybrid?: boolean }
}

interface SrPage {
  offset?: number
  limit?: number
  totalFound?: number
  content?: unknown
}

function postingUrl(token: string, p: SrPosting): string {
  return `https://jobs.smartrecruiters.com/${token}/${p.id ?? ""}`
}

export function parsePage(body: string, token: string): {
  postings: RawPosting[]
  identity: string | null
  totalFound: number
} {
  let data: SrPage
  try {
    data = JSON.parse(body)
  } catch {
    throw new SchemaError("smartrecruiters: response is not JSON")
  }
  if (!data || !Array.isArray(data.content) || typeof data.totalFound !== "number") {
    throw new SchemaError("smartrecruiters: payload has no `content` array / `totalFound`")
  }
  let identity: string | null = null
  const postings: RawPosting[] = []
  for (const raw of data.content as SrPosting[]) {
    if (!raw || typeof raw !== "object") continue
    const id = (raw.id ?? raw.uuid ?? "").trim()
    if (!id) continue
    if (!identity && raw.company?.identifier) identity = raw.company.identifier
    const loc = raw.location ?? {}
    const parts = [loc.city, loc.region].map((s) => (s ?? "").trim()).filter(Boolean)
    postings.push({
      posting_id: id,
      title: (raw.name ?? "").trim() || "(untitled)",
      company: raw.company?.name ?? null,
      // Lowercase ISO-2 on the wire ("nl"); the normalizer upper-cases it.
      locations: hints([[parts.join(", "), loc.country ?? null]]),
      workplace: loc.remote ? "remote" : loc.hybrid ? "hybrid" : null,
      date: isoDate(raw.releasedDate),
      updated_at: null,
      url: postingUrl(token, raw),
      description: null,
      deadline: null,
      listed: true,
    })
  }
  return { postings, identity, totalFound: data.totalFound }
}

export const smartrecruiters: Vendor = {
  name: "smartrecruiters",
  hasVendorIdentity: true,
  inlineDescriptions: false,
  boardUrl: (token) => `https://jobs.smartrecruiters.com/${token}`,
  endpoint: (token) => `${API}/${token}/postings?limit=${pageSize()}&offset=0`,
  detectToken: (html) =>
    firstMatch(html, [
      /jobs\.smartrecruiters\.com\/([A-Za-z0-9_-]+)/i,
      /careers\.smartrecruiters\.com\/([A-Za-z0-9_-]+)/i,
      /api\.smartrecruiters\.com\/v1\/companies\/([A-Za-z0-9_-]+)/i,
    ]),

  async fetchBoard(token, get) {
    const postings: RawPosting[] = []
    let identity: string | null = null
    let requests = 0
    let offset = 0
    let total = 0
    const limit = pageSize()

    for (let page = 0; page < MAX_PAGES; page++) {
      const res = await get(`${API}/${token}/postings?limit=${limit}&offset=${offset}`)
      requests++
      const cls = classifyStatus(res.status)
      if (cls !== "success") {
        // A page that fails after earlier pages succeeded is a partial board, not
        // a failed one - and it must never be reported as `ok`.
        if (postings.length) {
          return { status: "success_partial", postings, identity, message: `page ${page + 1}: HTTP ${res.status}`, requests }
        }
        return { status: cls, postings: [], identity: null, message: `HTTP ${res.status}`, requests }
      }
      let parsed
      try {
        parsed = parsePage(res.body, token)
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e)
        if (postings.length) return { status: "success_partial", postings, identity, message, requests }
        return { status: "schema_changed", postings: [], identity: null, message, requests }
      }
      total = parsed.totalFound
      if (!identity) identity = parsed.identity
      postings.push(...parsed.postings)
      // The false-200: a slug that does not exist answers 200 with totalFound 0.
      if (total === 0) {
        return {
          status: "not_found",
          postings: [],
          identity: null,
          message: "200 with totalFound 0 - SmartRecruiters answers any unknown slug this way, so this is an unresolved board, not an empty one",
          requests,
        }
      }
      offset += limit
      if (offset >= total || parsed.postings.length === 0) break
    }

    if (postings.length < total) {
      return {
        status: "success_partial",
        postings,
        identity,
        message: `stopped after ${MAX_PAGES} pages with ${postings.length}/${total} postings`,
        requests,
      }
    }
    return { status: postings.length ? "ok" : "empty", postings, identity, message: null, requests }
  },

  async fetchDetail(token, postingId, get) {
    const res = await get(`${API}/${token}/postings/${postingId}`)
    if (classifyStatus(res.status) !== "success") return null
    let raw: SrPosting & { jobAd?: { sections?: Record<string, { title?: string; text?: string }> } }
    try {
      raw = JSON.parse(res.body)
    } catch {
      return null
    }
    const sections = raw.jobAd?.sections ?? {}
    const description =
      Object.values(sections)
        .map((s) => [s?.title, stripHtml(s?.text ?? null)].filter(Boolean).join("\n"))
        .filter(Boolean)
        .join("\n\n") || null
    const base = parsePage(JSON.stringify({ totalFound: 1, content: [raw] }), token).postings[0]
    return base ? { ...base, description } : null
  },
}
