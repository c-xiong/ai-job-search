// Greenhouse job board API. One GET returns the whole board, and `content=true`
// ships each posting's description with it - so a board fetch costs one request
// and `detail` never has to be looped over search hits.
//
// Verified live 2026-08-18 against boards-api.greenhouse.io/v1/boards/parloa/jobs.

import { SchemaError, classifyStatus, isoDate, stripHtml } from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import type { BoardResult, RawPosting } from "../types.ts"
import { firstMatch, hints, type Vendor } from "./vendor.ts"

const API = "https://boards-api.greenhouse.io/v1/boards"

interface GhJob {
  id?: number | string
  title?: string
  company_name?: string
  absolute_url?: string
  requisition_id?: string
  updated_at?: string
  /** The posting date. `updated_at` is a different fact and is never substituted. */
  first_published?: string
  application_deadline?: string | null
  location?: { name?: string } | null
  /** Present, and deliberately unused - see the note in parseBoard. */
  offices?: Array<{ name?: string }>
  content?: string
}

export function parseBoard(body: string, token: string): { postings: RawPosting[]; identity: string | null } {
  let data: { jobs?: unknown }
  try {
    data = JSON.parse(body)
  } catch {
    throw new SchemaError("greenhouse: response is not JSON")
  }
  if (!data || !Array.isArray(data.jobs)) {
    throw new SchemaError("greenhouse: payload has no `jobs` array")
  }
  let identity: string | null = null
  const postings: RawPosting[] = []
  for (const raw of data.jobs as GhJob[]) {
    if (!raw || typeof raw !== "object") continue
    const id = raw.id === undefined || raw.id === null ? "" : String(raw.id)
    if (!id) continue
    if (!identity && raw.company_name) identity = raw.company_name
    // `location.name` is the authoritative, and only, place string. It already
    // carries multi-location postings ("Berlin Office; Munich Office; Remotely in
    // Germany"), and the location model splits it.
    //
    // `offices[].name` is deliberately NOT used. On the live Parloa board it
    // holds "Berlin HQ" and "Parloa Inc." - an office *entity*, not a place. Read
    // as a location it did two kinds of damage: "Parloa Inc." resolved to nothing
    // and made New York postings look geographically uncertain rather than
    // out-of-scope, and "Berlin HQ" attached to a France/Paris posting made a
    // French role qualify as a Berlin one.
    // Greenhouse publishes no country field at all; the country is inferred
    // from the location text by the normalizer.
    const locations = hints([[raw.location?.name, null]])
    postings.push({
      posting_id: id,
      title: (raw.title ?? "").trim() || "(untitled)",
      company: raw.company_name ?? null,
      locations,
      workplace: null,
      // Greenhouse hands out both dates; `first_published` is the posting date and
      // `updated_at` is kept beside it. Substituting one for the other would make
      // an old posting look new on every content edit.
      date: isoDate(raw.first_published),
      updated_at: isoDate(raw.updated_at),
      url: raw.absolute_url ?? `${API}/${token}/jobs/${id}`,
      // Greenhouse double-escapes the body: the JSON string holds HTML entities
      // that decode into HTML, which then has to be stripped.
      description: stripHtml(raw.content ?? null),
      deadline: isoDate(raw.application_deadline ?? null),
      listed: true,
    })
  }
  return { postings, identity }
}

export const greenhouse: Vendor = {
  name: "greenhouse",
  hasVendorIdentity: true,
  inlineDescriptions: true,
  boardUrl: (token) => `https://job-boards.greenhouse.io/${token}`,
  endpoint: (token) => `${API}/${token}/jobs?content=true`,
  detectToken: (html) =>
    firstMatch(html, [
      /job-boards(?:\.eu)?\.greenhouse\.io\/(?:embed\/job_board\?for=)?([A-Za-z0-9_-]+)/i,
      /boards\.greenhouse\.io\/(?:embed\/job_board\?for=)?([A-Za-z0-9_-]+)/i,
      /boards-api\.greenhouse\.io\/v1\/boards\/([A-Za-z0-9_-]+)/i,
      /greenhouse\.io\/embed\/job_board\/js\?for=([A-Za-z0-9_-]+)/i,
    ]),

  async fetchBoard(token, get) {
    const res = await get(greenhouse.endpoint(token))
    const cls = classifyStatus(res.status)
    if (cls !== "success") {
      return { status: cls, postings: [], identity: null, message: `HTTP ${res.status}`, requests: 1 }
    }
    try {
      const { postings, identity } = parseBoard(res.body, token)
      return {
        status: postings.length ? "ok" : "empty",
        postings,
        identity,
        message: null,
        requests: 1,
      }
    } catch (e) {
      return {
        status: "schema_changed",
        postings: [],
        identity: null,
        message: e instanceof Error ? e.message : String(e),
        requests: 1,
      }
    }
  },

  async fetchDetail(token, postingId, get) {
    const res = await get(`${API}/${token}/jobs/${postingId}?questions=false`)
    if (classifyStatus(res.status) !== "success") return null
    try {
      const one = parseBoard(JSON.stringify({ jobs: [JSON.parse(res.body)] }), token)
      return one.postings[0] ?? null
    } catch {
      return null
    }
  },
}
