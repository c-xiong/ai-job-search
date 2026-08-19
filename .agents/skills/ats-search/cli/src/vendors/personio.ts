// Personio XML feed. Zero-dependency parsing, so the XML is split into <position>
// chunks and each chunk parsed independently - one malformed posting cannot take
// the board down with it (the chunked-parsing rule from the portal-skill contract).
//
// Verified live 2026-08-18 against unique.jobs.personio.de/xml. Two traps confirmed
// there: a non-customer subdomain answers 307 (not 404), and the subdomain proves
// only that *a* subdomain exists - `unique.jobs.personio.de` belongs to "unique land
// use GmbH", not to the Zurich AI company of the same name. That is precisely why
// §8 refuses to treat a working slug as identity evidence.

import { SchemaError, classifyStatus, decodeEntities, isoDate, stripHtml } from "../helpers.ts"
import type { Transport } from "../helpers.ts"
import type { BoardResult, RawPosting } from "../types.ts"
import { firstMatch, hints, type Vendor } from "./vendor.ts"

function host(token: string): string {
  return `https://${token}.jobs.personio.de`
}

/** Text of the first <tag>…</tag>, CDATA unwrapped and entities decoded. */
function tag(chunk: string, name: string): string | null {
  const m = chunk.match(new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, "i"))
  if (!m) return null
  return unwrap(m[1])
}

function unwrap(value: string): string {
  const cdata = value.match(/^\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*$/)
  const inner = cdata ? cdata[1] : value
  return decodeEntities(inner).trim()
}

export function parseBoard(body: string, token: string): { postings: RawPosting[]; identity: string | null } {
  if (!/<workzag-jobs[\s>]/i.test(body) && !/<position[\s>]/i.test(body)) {
    throw new SchemaError("personio: response is not a workzag-jobs XML feed")
  }
  const postings: RawPosting[] = []
  const chunks = body.match(/<position(?:\s[^>]*)?>[\s\S]*?<\/position>/gi) ?? []
  for (const chunk of chunks) {
    // The descriptions block also contains <name> elements; strip it before
    // reading the position's own scalar fields so the job title cannot be
    // silently replaced by a description section heading.
    const descBlock = chunk.match(/<jobDescriptions>[\s\S]*?<\/jobDescriptions>/i)?.[0] ?? ""
    const scalars = chunk.replace(/<jobDescriptions>[\s\S]*?<\/jobDescriptions>/i, "")
    const id = tag(scalars, "id")
    if (!id) continue
    const office = tag(scalars, "office")
    const description = descBlock
      ? (descBlock.match(/<value(?:\s[^>]*)?>[\s\S]*?<\/value>/gi) ?? [])
          .map((v) => stripHtml(unwrap(v.replace(/^<value(?:\s[^>]*)?>/i, "").replace(/<\/value>$/i, ""))))
          .filter(Boolean)
          .join("\n\n") || null
      : null
    postings.push({
      posting_id: id,
      title: tag(scalars, "name") || "(untitled)",
      company: tag(scalars, "subcompany") || null,
      locations: hints([[office, null]]),
      workplace: null,
      date: isoDate(tag(scalars, "createdAt")),
      updated_at: null,
      url: `${host(token)}/job/${id}`,
      description,
      deadline: null,
      listed: true,
    })
  }
  return { postings, identity: null }
}

export const personio: Vendor = {
  name: "personio",
  hasVendorIdentity: false,
  inlineDescriptions: true,
  boardUrl: (token) => host(token),
  endpoint: (token) => `${host(token)}/xml`,
  detectToken: (html) =>
    firstMatch(html, [
      /([A-Za-z0-9-]+)\.jobs\.personio\.(?:de|com)/i,
      /personio\.de\/(?:job-board|xml)\/([A-Za-z0-9-]+)/i,
    ]),

  async fetchBoard(token, get) {
    const res = await get(personio.endpoint(token), { accept: "application/xml" })
    const cls = classifyStatus(res.status)
    if (cls !== "success") {
      // A 307 here is Personio bouncing a subdomain that was never a customer.
      const note = res.status >= 300 && res.status < 400 ? `HTTP ${res.status} (redirect: no such board)` : `HTTP ${res.status}`
      return { status: cls, postings: [], identity: null, message: note, requests: 1 }
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
    const board = await personio.fetchBoard(token, get)
    return board.postings.find((p) => p.posting_id === postingId) ?? null
  },
}
