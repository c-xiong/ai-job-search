// Ashby posting API. One GET, `descriptionPlain` inline, and a structured address
// - but no company identifier anywhere in the payload, so an Ashby board can only
// reach `verified` through the company's own site or a human (§8).
//
// Verified live 2026-08-18 against api.ashbyhq.com/posting-api/job-board/alephalpha.

import { SchemaError, classifyStatus, isoDate } from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import type { BoardResult, RawPosting } from "../types.ts"
import { firstMatch, hints, type Vendor } from "./vendor.ts"

const API = "https://api.ashbyhq.com/posting-api/job-board"

interface AshbyAddress {
  postalAddress?: { addressCountry?: string; addressLocality?: string; addressRegion?: string }
}

interface AshbyJob {
  id?: string
  title?: string
  location?: string
  secondaryLocations?: Array<{ location?: string; address?: AshbyAddress } | string>
  publishedAt?: string
  isListed?: boolean
  isRemote?: boolean
  workplaceType?: string
  address?: AshbyAddress
  jobUrl?: string
  applyUrl?: string
  descriptionPlain?: string
}

export function parseBoard(body: string, token: string): { postings: RawPosting[]; identity: string | null } {
  let data: { jobs?: unknown }
  try {
    data = JSON.parse(body)
  } catch {
    throw new SchemaError("ashby: response is not JSON")
  }
  if (!data || !Array.isArray(data.jobs)) {
    throw new SchemaError("ashby: payload has no `jobs` array")
  }
  const postings: RawPosting[] = []
  for (const raw of data.jobs as AshbyJob[]) {
    if (!raw || typeof raw !== "object") continue
    const id = (raw.id ?? "").trim()
    if (!id) continue
    // `isListed: false` is a posting the company has taken off its board; honoring
    // it is the difference between the board we monitor and the board they publish.
    if (raw.isListed === false) continue
    // Each secondary location carries (or does not carry) its own address. The
    // primary's country must not be spread across them: a New York primary with
    // a Berlin secondary would otherwise normalize Berlin as US and drop it.
    const secondary: Array<[string | undefined, string | null]> = (raw.secondaryLocations ?? []).map((s) =>
      typeof s === "string"
        ? [s, null]
        : [s?.location, s?.address?.postalAddress?.addressCountry ?? null],
    )
    const locations = hints([
      // A country name ("Germany"), not a code - the normalizer resolves both.
      // Useful for the country filter and, per §8, never identity evidence.
      [raw.location, raw.address?.postalAddress?.addressCountry ?? null],
      ...secondary,
    ])
    const workplace = (raw.workplaceType ?? "").trim() || (raw.isRemote ? "Remote" : "")
    postings.push({
      posting_id: id,
      title: (raw.title ?? "").trim() || "(untitled)",
      company: null,
      locations,
      workplace: workplace || null,
      date: isoDate(raw.publishedAt),
      updated_at: null,
      url: raw.jobUrl ?? `https://jobs.ashbyhq.com/${token}/${id}`,
      description: (raw.descriptionPlain ?? "").trim() || null,
      deadline: null,
      listed: true,
    })
  }
  return { postings, identity: null }
}

export const ashby: Vendor = {
  name: "ashby",
  hasVendorIdentity: false,
  inlineDescriptions: true,
  boardUrl: (token) => `https://jobs.ashbyhq.com/${token}`,
  endpoint: (token) => `${API}/${token}`,
  detectToken: (html) =>
    firstMatch(html, [
      /jobs\.ashbyhq\.com\/([A-Za-z0-9_.-]+)/i,
      /api\.ashbyhq\.com\/posting-api\/job-board\/([A-Za-z0-9_.-]+)/i,
      /embed\.ashbyhq\.com\/([A-Za-z0-9_.-]+)/i,
    ]),

  async fetchBoard(token, get) {
    const res = await get(ashby.endpoint(token))
    const cls = classifyStatus(res.status)
    if (cls !== "success") {
      return { status: cls, postings: [], identity: null, message: `HTTP ${res.status}`, requests: 1 }
    }
    try {
      const { postings } = parseBoard(res.body, token)
      return { status: postings.length ? "ok" : "empty", postings, identity: null, message: null, requests: 1 }
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

  // No per-posting endpoint exists, and none is needed: the board already carries
  // every description, so a detail lookup is one board fetch plus a local find.
  async fetchDetail(token, postingId, get) {
    const board = await ashby.fetchBoard(token, get)
    return board.postings.find((p) => p.posting_id === postingId) ?? null
  },
}
