// Shared shapes. Kept in one file because the vendor adapters, the pure filter
// layer and the Python side all have to agree on them, and a drifting field name
// is exactly the failure the fixture tests exist to catch.

export const VENDORS = ["greenhouse", "ashby", "personio", "lever", "smartrecruiters", "workday"] as const
export type VendorName = (typeof VENDORS)[number]

/**
 * Per-company outcome of one board fetch. Borrowed from DESIGN_V3 §5.3: a
 * structured status, never an exception and never an empty list standing in for
 * a failure. `empty` (reachable, genuinely zero postings) and `not_found` (the
 * board does not exist) are deliberately never collapsed - one means "nothing
 * to do", the other means "the token is wrong".
 */
export const COMPANY_STATUSES = [
  "ok",
  "empty",
  "not_found",
  "rate_limited",
  "schema_changed",
  "unavailable",
  "success_partial",
] as const
export type CompanyStatus = (typeof COMPANY_STATUSES)[number]

/**
 * One location a posting names, with the country the vendor attached to *that*
 * location. Per-location rather than per-posting: an Ashby posting with a New
 * York primary and a Berlin secondary carries one `address`, and applying it to
 * both locations turned the Berlin option into a US one and dropped it.
 */
export interface LocationHint {
  text: string
  /** The vendor's own country value for this location, verbatim ("Germany",
   *  "nl", "GB") - normalized later. Null when the vendor did not say. */
  country: string | null
}

/** A posting as the vendor states it, before any filtering or normalization. */
export interface RawPosting {
  posting_id: string
  title: string
  /** From the payload when the vendor exposes it per posting (Merantix's venture
   *  studio lists portfolio companies), otherwise null and the registry name wins. */
  company: string | null
  /** Every location the posting names; index 0 is the vendor's primary. */
  locations: LocationHint[]
  /** The vendor's own workplace value, verbatim ("Hybrid", "onsite") - normalized later. */
  workplace: string | null
  /** Posting date as ISO YYYY-MM-DD, or null. Never synthesized from now(). */
  date: string | null
  updated_at: string | null
  url: string
  description: string | null
  deadline: string | null
  /** Vendor "is this posting published" flag; unlisted postings are dropped. */
  listed: boolean
}

/** What one board fetch produced. */
export interface BoardResult {
  status: CompanyStatus
  postings: RawPosting[]
  /** An independent company identifier from the payload, where the vendor has one
   *  (greenhouse `company_name`, smartrecruiters `company.identifier`). Null is
   *  not a failure - three of five vendors simply do not publish one (§8). */
  identity: string | null
  message: string | null
  requests: number
}

/** Normalized geography for one posting. */
export interface Place {
  raw: string
  city: string | null
  /** ISO-3166 alpha-2, uppercase, or null when nothing resolved it. */
  country: string | null
  /** A macro-region named by a remote posting ("europe", "emea", ...), else null. */
  region: string | null
}

export type Workplace = "onsite" | "hybrid" | "remote"

/**
 * A search result. The first six fields are the portal-skill contract; the rest
 * are the permitted superset the ATS boards make available for free.
 */
export interface JobResult {
  /** Composite: `<vendor>:<token>:<posting_id>` (§10.2). */
  id: string
  title: string
  company: string | null
  location: string | null
  date: string | null
  url: string

  /** The employer as the registry names it. Distinct from `company`, which a
   *  venture studio's board (Merantix) fills with the portfolio company - so
   *  anything attributing a result back to a registry row must use this. */
  registry_company: string
  vendor: VendorName
  token: string
  tier: number
  /** Every location the posting names, normalized for display. */
  locations: string[]
  city: string | null
  country: string | null
  region: string | null
  workplace: Workplace
  /** True when geography could not be resolved: surfaced and flagged, never
   *  silently treated as qualifying (§9). */
  location_uncertain: boolean
  updated_at: string | null
  deadline: string | null
  description: string | null
  prefit_score: number
  prefit_reasons: string[]
  /** Per-posting notes from the filter layer, e.g. `werkstudent-country-unknown`. */
  flags: string[]
}

/** Per-company row in `meta.companies[]`. */
export interface CompanyReport {
  name: string
  vendor: VendorName | null
  token: string | null
  status: CompanyStatus
  message: string | null
  /** Postings the board returned, before filtering. */
  jobs_seen: number
  /** Postings that survived every filter. */
  eligible: number
  requests: number
  /** True when the payload came from the §2 per-company cooldown cache. */
  cached: boolean
}

export interface SearchMeta {
  count: number
  page: number
  /** True when at least one selected company failed but at least one succeeded. */
  degraded: boolean
  status_counts: Record<string, number>
  companies: CompanyReport[]
  requests: number
  /** Results whose composite id was not in `--known-ids`. What §2's row bound counts. */
  new_rows: number
  generated_at: string
  note?: string
}

export interface SearchPayload {
  meta: SearchMeta
  results: JobResult[]
}
