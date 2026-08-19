// The adapter contract, in its own module so the adapters can import it without
// importing the registry of adapters - a cycle Bun resolves at runtime by handing
// one side an uninitialized binding.

import type { BoardResult, LocationHint, RawPosting, VendorName } from "../types.ts"
import type { Transport } from "../helpers.ts"

export interface Vendor {
  name: VendorName
  /** The board page a human opens. */
  boardUrl(token: string): string
  /** The endpoint a board fetch hits first (also what `resolve` probes). */
  endpoint(token: string): string
  /** True when the payload carries an independent company identifier (§8 rank 2). */
  hasVendorIdentity: boolean
  /** Does this vendor ship descriptions with the board listing? */
  inlineDescriptions: boolean
  /** Find this vendor's board token in a careers page's markup. */
  detectToken(html: string): string | null
  /** Fetch (and paginate) one board. Never throws: failures come back as a status. */
  fetchBoard(token: string, get: Transport): Promise<BoardResult>
  /** One posting's full detail. Null when the posting is gone. */
  fetchDetail(token: string, postingId: string, get: Transport): Promise<RawPosting | null>
}

/** Build a deduplicated location list, dropping blanks. */
export function hints(entries: Array<[string | null | undefined, string | null | undefined]>): LocationHint[] {
  const out: LocationHint[] = []
  for (const [text, country] of entries) {
    const t = (text ?? "").trim()
    if (!t || out.some((h) => h.text === t)) continue
    out.push({ text: t, country: (country ?? null) || null })
  }
  return out
}

/** First capture group of the first pattern that matches. */
export function firstMatch(html: string, patterns: RegExp[]): string | null {
  for (const re of patterns) {
    const m = html.match(re)
    if (m && m[1]) return m[1]
  }
  return null
}
