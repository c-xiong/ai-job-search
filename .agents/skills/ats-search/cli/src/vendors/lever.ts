// Lever postings API. A bare JSON array, descriptions inline, an ISO-2 `country`
// and an epoch-millisecond `createdAt` - the one vendor whose date is not a string.
//
// Verified live 2026-08-18 against api.lever.co/v0/postings/sonarsource.

import { SchemaError, classifyStatus, isoDate, stripHtml } from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import type { BoardResult, RawPosting } from "../types.ts"
import { firstMatch, hints, type Vendor } from "./vendor.ts"

const API = "https://api.lever.co/v0/postings"

interface LeverList {
  text?: string
  content?: string
}

interface LeverPosting {
  id?: string
  text?: string
  hostedUrl?: string
  applyUrl?: string
  createdAt?: number | string
  country?: string
  workplaceType?: string
  categories?: { commitment?: string; department?: string; location?: string; team?: string; allLocations?: string[] }
  descriptionPlain?: string
  additionalPlain?: string
  lists?: LeverList[]
}

function fullText(raw: LeverPosting): string | null {
  const parts = [raw.descriptionPlain ?? ""]
  for (const list of raw.lists ?? []) {
    if (list?.text) parts.push(list.text)
    const content = stripHtml(list?.content ?? null)
    if (content) parts.push(content)
  }
  if (raw.additionalPlain) parts.push(raw.additionalPlain)
  const joined = parts.filter(Boolean).join("\n\n").trim()
  return joined || null
}

export function parseBoard(body: string, token: string): { postings: RawPosting[]; identity: string | null } {
  let data: unknown
  try {
    data = JSON.parse(body)
  } catch {
    throw new SchemaError("lever: response is not JSON")
  }
  if (!Array.isArray(data)) throw new SchemaError("lever: payload is not an array of postings")
  const postings: RawPosting[] = []
  for (const raw of data as LeverPosting[]) {
    if (!raw || typeof raw !== "object") continue
    const id = (raw.id ?? "").trim()
    if (!id) continue
    const all = raw.categories?.allLocations ?? []
    // `country` is the posting's ISO-2 and belongs to the primary location only;
    // `allLocations` can name places in other countries.
    const locations = hints([
      [raw.categories?.location, raw.country ?? null],
      ...all.map((s): [string | undefined, string | null] => [s, null]),
    ])
    postings.push({
      posting_id: id,
      title: (raw.text ?? "").trim() || "(untitled)",
      company: null,
      locations,
      workplace: raw.workplaceType ?? null,
      // Epoch milliseconds, not a string. isoDate() takes both.
      date: isoDate(raw.createdAt),
      updated_at: null,
      url: raw.hostedUrl ?? `https://jobs.lever.co/${token}/${id}`,
      description: fullText(raw),
      deadline: null,
      listed: true,
    })
  }
  return { postings, identity: null }
}

export const lever: Vendor = {
  name: "lever",
  hasVendorIdentity: false,
  inlineDescriptions: true,
  boardUrl: (token) => `https://jobs.lever.co/${token}`,
  endpoint: (token) => `${API}/${token}?mode=json`,
  detectToken: (html) =>
    firstMatch(html, [
      /jobs\.(?:eu\.)?lever\.co\/([A-Za-z0-9_-]+)/i,
      /api\.(?:eu\.)?lever\.co\/v0\/postings\/([A-Za-z0-9_-]+)/i,
    ]),

  async fetchBoard(token, get) {
    const res = await get(lever.endpoint(token))
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

  async fetchDetail(token, postingId, get) {
    const res = await get(`${API}/${token}/${postingId}?mode=json`)
    if (classifyStatus(res.status) !== "success") return null
    try {
      const { postings } = parseBoard(`[${res.body}]`, token)
      return postings[0] ?? null
    } catch {
      return null
    }
  },
}
