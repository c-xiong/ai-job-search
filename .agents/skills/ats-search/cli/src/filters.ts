// The filter layer. Everything here is pure: same input plus same config gives the
// same output and the same order, with no clock, no network and no file access -
// which is what makes the whole selection story unit-testable offline (§9).

import type { LocationHint, Place, RawPosting, Workplace } from "./types.ts"

// ---------------------------------------------------------------------------
// Normalization tables. A table, not `includes()` (§9): substring matching is how
// "Remote — US only" slips through a filter configured for DACH.
// ---------------------------------------------------------------------------

const CITY_TABLE: Record<string, { city: string; country: string }> = {}
function city(country: string, canonical: string, ...aliases: string[]): void {
  for (const alias of [canonical, ...aliases]) CITY_TABLE[alias.toLowerCase()] = { city: canonical, country }
}
city("CH", "Zurich", "Zürich", "Zuerich", "Zurich, Switzerland", "Zürich, Schweiz")
city("CH", "Zug")
city("CH", "Basel", "Bâle", "Basle")
city("CH", "Bern", "Berne")
city("CH", "Geneva", "Genève", "Geneve", "Genf", "Ginevra")
city("CH", "Lausanne")
city("CH", "Winterthur")
city("CH", "Lucerne", "Luzern")
city("CH", "St. Gallen", "St Gallen", "Sankt Gallen")
city("CH", "Baden")
city("CH", "Gland")
city("CH", "Stäfa", "Staefa")
city("CH", "Lugano")
city("DE", "Berlin")
city("DE", "Munich", "München", "Muenchen")
city("DE", "Hamburg")
city("DE", "Cologne", "Köln", "Koeln")
city("DE", "Frankfurt", "Frankfurt am Main", "Frankfurt/Main")
city("DE", "Heidelberg")
city("DE", "Stuttgart")
city("DE", "Düsseldorf", "Duesseldorf", "Dusseldorf")
city("DE", "Karlsruhe")
city("DE", "Leipzig")
city("DE", "Dresden")
city("DE", "Ulm")
city("DE", "Walldorf")
city("DE", "Nuremberg", "Nürnberg", "Nuernberg")
city("AT", "Vienna", "Wien")
city("AT", "Linz")
city("AT", "Graz")
city("AT", "Salzburg")
city("GB", "London")
city("GB", "Cambridge")
city("GB", "Manchester")
city("PL", "Warsaw", "Warszawa")
city("PL", "Krakow", "Kraków", "Cracow")
city("PT", "Lisbon", "Lisboa")
city("PT", "Porto")
city("NL", "Amsterdam")
city("NL", "Utrecht")
city("IE", "Dublin")
city("FR", "Paris")
city("NO", "Oslo")
city("SE", "Stockholm")
city("DK", "Copenhagen", "København", "Kobenhavn")
city("IT", "Milan", "Milano")
city("IT", "Padova", "Padua")
city("IT", "Rome", "Roma")
city("BE", "Brussels", "Bruxelles", "Brussel")
city("BE", "Leuven", "Louvain")
city("LU", "Luxembourg City", "Luxembourg-Ville")
city("ES", "Madrid")
city("ES", "Barcelona")
city("GR", "Athens", "Athina")
city("CZ", "Prague", "Praha")
city("US", "New York", "New York City", "NYC", "New York Office")
city("US", "San Francisco")
city("US", "Seattle")
city("US", "Boston")
city("US", "Austin")
city("CA", "Toronto")
city("CA", "Vancouver")
city("IN", "Bangalore", "Bengaluru")
city("SG", "Singapore City")
city("JP", "Tokyo")
city("AU", "Sydney")
city("IL", "Tel Aviv")

const COUNTRY_TABLE: Record<string, string> = {}
function country(code: string, ...aliases: string[]): void {
  COUNTRY_TABLE[code.toLowerCase()] = code
  for (const alias of aliases) COUNTRY_TABLE[alias.toLowerCase()] = code
}
country("CH", "Switzerland", "Schweiz", "Suisse", "Svizzera")
country("DE", "Germany", "Deutschland", "Allemagne", "DEU")
country("AT", "Austria", "Österreich", "Oesterreich")
country("GB", "United Kingdom", "UK", "England", "Great Britain", "Scotland", "Wales")
country("PL", "Poland", "Polska")
country("PT", "Portugal")
country("NL", "Netherlands", "Nederland", "The Netherlands", "Holland")
country("IE", "Ireland")
country("FR", "France")
country("NO", "Norway", "Norge")
country("SE", "Sweden", "Sverige")
country("DK", "Denmark", "Danmark")
country("IT", "Italy", "Italia")
country("BE", "Belgium", "Belgique", "België")
country("LU", "Luxembourg")
country("ES", "Spain", "España", "Espana")
country("GR", "Greece")
country("CZ", "Czechia", "Czech Republic")
country("RO", "Romania")
country("BG", "Bulgaria")
country("UA", "Ukraine")
country("TR", "Turkey", "Türkiye")
country("US", "United States", "USA", "United States of America", "U.S.", "U.S.A.")
country("CA", "Canada")
country("IN", "India")
country("SG", "Singapore")
country("JP", "Japan")
country("AU", "Australia")
country("IL", "Israel")
country("BR", "Brazil", "Brasil")
country("MX", "Mexico", "México")
country("AE", "United Arab Emirates", "UAE")

/** Macro-regions a remote posting may name instead of a country. */
const REGION_TABLE: Record<string, string> = {
  europe: "europe",
  european: "europe",
  "european union": "eu",
  eu: "eu",
  "eu only": "eu",
  eea: "eu",
  dach: "dach",
  emea: "emea",
  worldwide: "worldwide",
  global: "worldwide",
  anywhere: "worldwide",
  international: "worldwide",
  apac: "apac",
  americas: "americas",
  latam: "latam",
  "north america": "north america",
  us: "us",
  usa: "us",
}

const SPLIT = /\s*(?:,|\||\/|;|—|–| - |\(|\)|\bor\b|\band\b)\s*/i

/**
 * Second pass over a whole segment, for the strings that are a sentence rather
 * than a list: "Remote in Europe", "Anywhere in Switzerland". Only keys long
 * enough to be unambiguous are scanned - a bare "us" or "in" inside prose is
 * never read as a country, which is exactly the class of error the table exists
 * to prevent. Two-letter codes still resolve when they are their own segment.
 */
const PLACE_SUBSTRINGS = [...Object.keys(CITY_TABLE), ...Object.keys(COUNTRY_TABLE)]
  .filter((k) => k.length >= 4)
  .sort((a, b) => b.length - a.length)
const REGION_SUBSTRINGS = Object.keys(REGION_TABLE)
  .filter((k) => k.length >= 3)
  .sort((a, b) => b.length - a.length)

function wordMatch(haystack: string, needle: string): boolean {
  return new RegExp(`(?:^|[^a-z0-9])${needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:[^a-z0-9]|$)`, "i").test(
    haystack,
  )
}

/** Split a location string into the tokens worth looking up. */
function tokenize(raw: string): string[] {
  return raw
    .split(SPLIT)
    .map((t) => t.trim().replace(/\.$/, ""))
    .filter(Boolean)
}

/**
 * One vendor location string can name several places: Greenhouse ships
 * "Berlin Office; Munich Office; Remotely in Germany" as a single `location.name`.
 * Split on the separators that mean "and also" - `;` and `|` - but never on a
 * comma, which is how a single place spells itself ("Munich, Germany").
 */
export function splitLocationList(hint: LocationHint): LocationHint[] {
  return (hint.text ?? "")
    .split(/[;|]/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((text) => ({ text, country: hint.country }))
}

/** Resolve one ISO-2 code / country name to an ISO-2 code, or null. */
export function toCountryCode(value: string | null | undefined): string | null {
  if (!value) return null
  return COUNTRY_TABLE[value.trim().toLowerCase()] ?? null
}

/**
 * Turn one raw location string into a structured place. `vendorCountry` is the
 * vendor's own country field where it has one (Ashby "Germany", Lever "GB",
 * SmartRecruiters "nl"); it wins over anything inferred from the text.
 */
export function normalizePlace(raw: string, vendorCountry?: string | null): Place {
  const text = (raw ?? "").trim()
  const tokens = tokenize(text)
  let resolvedCity: string | null = null
  let resolvedCountry = toCountryCode(vendorCountry)
  let region: string | null = null

  for (const token of tokens) {
    const key = token.toLowerCase()
    if (!resolvedCity && CITY_TABLE[key]) {
      resolvedCity = CITY_TABLE[key].city
      if (!resolvedCountry) resolvedCountry = CITY_TABLE[key].country
      continue
    }
    if (!region && REGION_TABLE[key]) {
      region = REGION_TABLE[key]
      continue
    }
    // A bare two-letter token is only a country code if it is its own segment;
    // matching it inside free text turns "Remote in Europe" into India.
    if (!resolvedCountry && COUNTRY_TABLE[key] && (key.length > 2 || token === token.toUpperCase())) {
      resolvedCountry = COUNTRY_TABLE[key]
    }
  }
  // Sentence-shaped locations, where nothing was its own comma-separated segment.
  if (!resolvedCity || !resolvedCountry) {
    const lower = text.toLowerCase()
    for (const key of PLACE_SUBSTRINGS) {
      if (!wordMatch(lower, key)) continue
      const cityHit = CITY_TABLE[key]
      if (cityHit) {
        if (!resolvedCity) resolvedCity = cityHit.city
        if (!resolvedCountry) resolvedCountry = cityHit.country
      } else if (!resolvedCountry) {
        resolvedCountry = COUNTRY_TABLE[key]
      }
      if (resolvedCity && resolvedCountry) break
    }
  }
  if (!region) {
    const lower = text.toLowerCase()
    for (const key of REGION_SUBSTRINGS) {
      if (wordMatch(lower, key)) {
        region = REGION_TABLE[key]
        break
      }
    }
  }

  // A region that is really a country ("US") resolves as the country instead.
  if (region && !resolvedCountry && (region === "us" || region === "north america")) {
    if (region === "us") resolvedCountry = "US"
  }
  return { raw: text, city: resolvedCity, country: resolvedCountry, region }
}

/** Vendor workplace field first, the location text only as a fallback. */
export function classifyWorkplace(vendorValue: string | null | undefined, locationText: string): Workplace {
  const v = (vendorValue ?? "").toLowerCase()
  if (v.includes("remote")) return "remote"
  if (v.includes("hybrid")) return "hybrid"
  if (v.includes("onsite") || v.includes("on-site") || v.includes("office")) return "onsite"
  const t = (locationText ?? "").toLowerCase()
  if (/\bremote\b|\bfully remote\b|\bwork from home\b/.test(t)) return "remote"
  if (/\bhybrid\b/.test(t)) return "hybrid"
  return "onsite"
}

// ---------------------------------------------------------------------------
// Title rules
// ---------------------------------------------------------------------------

function escapeRe(term: string): string {
  return term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

/**
 * Word-boundary term match, so `staff` cannot match "staffing" and `vp` cannot
 * match "VPN" - the substring version of this rule silently deleted whole
 * categories of eligible postings.
 */
export function matchesTerm(text: string, term: string): boolean {
  const t = term.trim()
  if (!t) return false
  return new RegExp(`(?:^|[^a-z0-9])${escapeRe(t)}(?:[^a-z0-9]|$)`, "i").test(text)
}

export function firstMatchingTerm(text: string, terms: string[]): string | null {
  for (const term of terms) if (matchesTerm(text, term)) return term
  return null
}

export interface ConditionalExclude {
  pattern: string
  unless_country?: string[]
  why?: string
}

export interface FilterConfig {
  allowed_countries: string[]
  remote_regions: string[]
  cities: string[]
  title_include: string[]
  title_exclude: string[]
  conditional_exclude: ConditionalExclude[]
}

export interface LocationVerdict {
  qualifies: boolean
  uncertain: boolean
  /** The place that qualified (or the primary one when nothing did). */
  matched: Place | null
  places: Place[]
}

/**
 * Multi-location postings qualify if **any** location qualifies, and the
 * qualifying location is what gets reported - Helsing's "Berlin; London; Munich;
 * Paris" is a Berlin job for our purposes, not a Paris one.
 */
export function locationVerdict(
  places: Place[],
  workplace: Workplace,
  cfg: Pick<FilterConfig, "allowed_countries" | "remote_regions">,
): LocationVerdict {
  const allowed = cfg.allowed_countries.map((c) => c.toUpperCase())
  const regions = cfg.remote_regions.map((r) => r.toLowerCase())
  let anyResolved = false
  const effective = places.length ? places : [{ raw: "", city: null, country: null, region: null }]

  for (const place of effective) {
    if (place.country) {
      anyResolved = true
      if (allowed.includes(place.country)) return { qualifies: true, uncertain: false, matched: place, places }
      continue // a resolved country outside the set is a real "no", not a maybe
    }
    if (place.region) {
      // A macro-region only qualifies a posting that is actually remote: §9
      // scopes `remote_regions` to "remote postings whose geography is a region
      // rather than a country". "EMEA" on an ONSITE role names a market, not a
      // place you could commute to - and it is not a resolved geography either,
      // so it leaves the posting uncertain (surfaced and flagged) rather than
      // dropped. Only a *remote* posting's region is a fact about where you
      // would be allowed to work.
      if (workplace !== "remote") continue
      anyResolved = true
      if (regions.includes(place.region)) {
        return { qualifies: true, uncertain: false, matched: place, places }
      }
      continue
    }
  }
  // Uncertainty means we resolved NOTHING - no country and no region anywhere on
  // the posting. A posting that stated a geography we can read, and it is not
  // ours, is a "no": an extra unreadable string next to it ("Parloa Inc.") must
  // not launder a New York role into a flagged maybe.
  return { qualifies: false, uncertain: !anyResolved, matched: effective[0] ?? null, places }
}

export interface FilteredPosting {
  posting: RawPosting
  places: Place[]
  matched: Place | null
  workplace: Workplace
  location_uncertain: boolean
  flags: string[]
  matched_title_term: string | null
}

export interface FilterOutcome {
  kept: FilteredPosting[]
  /** Why the rest went, by reason - a filter that quietly eats a board is a bug. */
  dropped: Record<string, number>
}

export interface FilterOpts {
  /** Drop postings older than this many days. A null date is always kept. */
  jobageDays?: number | null
  /** Reference date (ISO) for jobage. Passed in so the layer stays pure. */
  today?: string
}

/**
 * Apply every rule in the §9 order: published guard, title, conditional excludes,
 * location model, then age.
 */
export function applyFilters(
  postings: RawPosting[],
  cfg: FilterConfig,
  opts: FilterOpts = {},
): FilterOutcome {
  const dropped: Record<string, number> = {}
  const bump = (key: string) => {
    dropped[key] = (dropped[key] ?? 0) + 1
  }
  const kept: FilteredPosting[] = []

  for (const posting of postings) {
    if (!posting.listed) {
      bump("unlisted")
      continue
    }
    const title = posting.title
    const excludedTerm = firstMatchingTerm(title, cfg.title_exclude)
    if (excludedTerm) {
      bump("title_exclude")
      continue
    }
    const includedTerm = firstMatchingTerm(title, cfg.title_include)
    if (!includedTerm) {
      bump("title_include")
      continue
    }

    const locationText = posting.locations.map((l) => l.text).join(", ")
    const workplace = classifyWorkplace(posting.workplace, locationText)
    const rawPlaces = posting.locations.flatMap(splitLocationList)
    const places = (rawPlaces.length ? rawPlaces : [{ text: "", country: null }]).map((loc) =>
      normalizePlace(loc.text, loc.country),
    )
    const flags: string[] = []

    // Conditional excludes are evaluated against the *resolved* country, and an
    // unresolved country keeps the posting with a flag rather than guessing.
    let conditionalDrop = false
    for (const rule of cfg.conditional_exclude ?? []) {
      let re: RegExp
      try {
        re = new RegExp(rule.pattern, "i")
      } catch {
        continue // a broken pattern in the registry must not take the run down
      }
      if (!re.test(title)) continue
      const unless = (rule.unless_country ?? []).map((c) => c.toUpperCase())
      const countries = places.map((p) => p.country).filter(Boolean) as string[]
      if (countries.some((c) => unless.includes(c))) continue
      if (countries.length === 0) {
        flags.push("conditional-country-unknown")
        continue
      }
      conditionalDrop = true
      break
    }
    if (conditionalDrop) {
      bump("conditional_exclude")
      continue
    }

    const verdict = locationVerdict(places, workplace, cfg)
    if (!verdict.qualifies && !verdict.uncertain) {
      bump("location")
      continue
    }
    if (verdict.uncertain) flags.push("location-uncertain")

    if (opts.jobageDays != null && posting.date && opts.today) {
      const age = Math.floor((Date.parse(opts.today) - Date.parse(posting.date)) / 86400000)
      if (age > opts.jobageDays) {
        bump("jobage")
        continue
      }
    }
    if (!posting.date) flags.push("no-posting-date")

    kept.push({
      posting,
      places,
      matched: verdict.matched,
      workplace,
      location_uncertain: verdict.uncertain,
      flags,
      matched_title_term: includedTerm,
    })
  }
  return { kept, dropped }
}

/** The `-q` local filter: an OR over title, company and location. Never a request. */
export function localQuery(item: FilteredPosting, query: string): boolean {
  const terms = query.split(/\s+/).map((t) => t.trim().toLowerCase()).filter(Boolean)
  if (!terms.length) return true
  const hay = [item.posting.title, item.posting.company ?? "", item.posting.locations.map((l) => l.text).join(" ")]
    .join(" ")
    .toLowerCase()
  return terms.some((t) => hay.includes(t))
}
