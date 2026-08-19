// Tests 6, 8, 9, 10 of §14: the filter layer is pure, and every location branch
// behaves the way §9 says it does.

import { describe, expect, test } from "bun:test"
import {
  applyFilters,
  classifyWorkplace,
  locationVerdict,
  matchesTerm,
  normalizePlace,
  type FilterConfig,
} from "../src/filters.ts"
import { parseBoard as parseAshby } from "../src/vendors/ashby.ts"
import { parseBoard as parsePersonio } from "../src/vendors/personio.ts"
import { parseBoard as parseGreenhouse } from "../src/vendors/greenhouse.ts"
import { fixture } from "./helpers.ts"
import type { RawPosting } from "../src/types.ts"

const CFG: FilterConfig = {
  allowed_countries: ["CH", "DE", "AT"],
  remote_regions: ["europe", "eu", "dach", "emea"],
  cities: ["Zurich", "Berlin", "Munich", "Geneva"],
  title_include: ["engineer", "developer", "scientist", "ml", "machine learning", "ai", "nlp",
                  "llm", "software", "backend", "data", "research", "intern"],
  title_exclude: ["principal", "staff", "director", "head of", "vp", "chief", "sales", "recruiter"],
  conditional_exclude: [
    { pattern: "werkstudent|working student|studentische", unless_country: ["CH"] },
  ],
}

/** Location hints from plain strings, for readability in the cases below. */
function at(...texts: string[]) {
  return texts.map((text) => ({ text, country: null }))
}

function posting(over: Partial<RawPosting> = {}): RawPosting {
  return {
    posting_id: "1", title: "Software Engineer", company: null, locations: at("Zurich"),
    workplace: null, date: "2026-08-15", updated_at: null,
    url: "https://example.com/1", description: null, deadline: null, listed: true,
    ...over,
  }
}

describe("word-boundary title matching", () => {
  test("`staff` does not match staffing, `vp` does not match VPN", () => {
    expect(matchesTerm("Staffing Coordinator", "staff")).toBe(false)
    expect(matchesTerm("Staff Engineer", "staff")).toBe(true)
    expect(matchesTerm("VPN Infrastructure Engineer", "vp")).toBe(false)
    expect(matchesTerm("VP of Engineering", "vp")).toBe(true)
  })

  test("intern is an include term and thesis stays excluded", () => {
    const out = applyFilters(
      [posting({ posting_id: "a", title: "Machine Learning Intern" }),
       posting({ posting_id: "b", title: "Master Thesis Student" })],
      { ...CFG, title_exclude: [...CFG.title_exclude, "thesis"] },
    )
    expect(out.kept.map((k) => k.posting.posting_id)).toEqual(["a"])
  })
})

describe("test 6 - filters are pure", () => {
  test("same input and config give identical output and ordering, twice", () => {
    const input = parseAshby(fixture("ashby-deepjudge-20260818.json"), "deepjudge").postings
    const a = applyFilters(input, CFG, { jobageDays: null })
    const b = applyFilters(input, CFG, { jobageDays: null })
    expect(a.kept.map((k) => k.posting.posting_id)).toEqual(b.kept.map((k) => k.posting.posting_id))
    expect(a.dropped).toEqual(b.dropped)
  })

  test("the input array is not mutated", () => {
    const input = parseAshby(fixture("ashby-deepjudge-20260818.json"), "deepjudge").postings
    const before = JSON.stringify(input)
    applyFilters(input, CFG, { jobageDays: null })
    expect(JSON.stringify(input)).toBe(before)
  })
})

describe("normalization is a table, not includes()", () => {
  test("German, French and English spellings collapse onto one place", () => {
    expect(normalizePlace("Zürich")).toMatchObject({ city: "Zurich", country: "CH" })
    expect(normalizePlace("Genf")).toMatchObject({ city: "Geneva", country: "CH" })
    expect(normalizePlace("München, Deutschland")).toMatchObject({ city: "Munich", country: "DE" })
    expect(normalizePlace("Wien")).toMatchObject({ city: "Vienna", country: "AT" })
  })

  test("a vendor country field wins over anything inferred from text", () => {
    expect(normalizePlace("Remote", "Germany").country).toBe("DE")
    expect(normalizePlace("Remote", "nl").country).toBe("NL")
    expect(normalizePlace("Remote", "GB").country).toBe("GB")
  })

  test("a two-letter word inside free text is not read as a country code", () => {
    // "in" is not India.
    expect(normalizePlace("Work in Europe").country).toBeNull()
    expect(normalizePlace("Work in Europe").region).toBe("europe")
  })

  test("the vendor workplace field beats the location text", () => {
    expect(classifyWorkplace("Hybrid", "Remote")).toBe("hybrid")
    expect(classifyWorkplace(null, "Remote - Europe")).toBe("remote")
    expect(classifyWorkplace(null, "Zurich")).toBe("onsite")
  })
})

describe("test 10 - remote handling", () => {
  const places = (raw: string, vendorCountry?: string | null) => [normalizePlace(raw, vendorCountry)]

  test('"Remote (US)" is dropped', () => {
    const v = locationVerdict(places("Remote - United States", "United States"), "remote", CFG)
    expect(v.qualifies).toBe(false)
    expect(v.uncertain).toBe(false)
  })

  test('"Remote - Europe" qualifies', () => {
    const v = locationVerdict(places("Remote - Europe"), "remote", CFG)
    expect(v.qualifies).toBe(true)
    expect(v.matched?.region).toBe("europe")
  })

  test('a bare "Remote" is uncertain - surfaced and flagged, never qualifying', () => {
    const v = locationVerdict(places("Remote"), "remote", CFG)
    expect(v.qualifies).toBe(false)
    expect(v.uncertain).toBe(true)
  })

  test("end to end over the Ashby fixture: US dropped, Europe kept, bare Remote flagged", () => {
    const input = parseAshby(fixture("ashby-deepjudge-20260818.json"), "deepjudge").postings
    const out = applyFilters(input, CFG, { jobageDays: null })
    const ids = out.kept.map((k) => k.posting.posting_id)
    expect(ids).toContain("41c79388-fad4-4286-948c-b279c35c568d") // Zurich
    expect(ids).toContain("71c79388-fad4-4286-948c-b279c35c5003") // Remote - Europe
    expect(ids).toContain("51c79388-fad4-4286-948c-b279c35c5001") // bare Remote, flagged
    expect(ids).not.toContain("61c79388-fad4-4286-948c-b279c35c5002") // Remote - US
    const bare = out.kept.find((k) => k.posting.posting_id === "51c79388-fad4-4286-948c-b279c35c5001")!
    expect(bare.location_uncertain).toBe(true)
    expect(bare.flags).toContain("location-uncertain")
  })
})

describe("test 8 - multi-location postings", () => {
  test("qualify on any qualifying location, and report the one that qualified", () => {
    const out = applyFilters(
      [posting({ locations: at("Paris", "Munich", "London"), title: "Backend Engineer" })],
      CFG,
    )
    expect(out.kept.length).toBe(1)
    expect(out.kept[0].matched?.city).toBe("Munich")
    expect(out.kept[0].matched?.country).toBe("DE")
  })

  test("the greenhouse fixture's Paris+Munich posting is reported as Munich", () => {
    const input = parseGreenhouse(fixture("greenhouse-parloa-20260818.json"), "parloa").postings
    const out = applyFilters(input, CFG, { jobageDays: null })
    const multi = out.kept.find((k) => k.posting.posting_id === "4635890103")!
    expect(multi.matched?.city).toBe("Munich")
  })
})

describe("test 9 - the werkstudent conditional exclude", () => {
  const input = parsePersonio(fixture("personio-merantix-20260818.xml"), "merantix").postings
  const out = applyFilters(input, CFG, { jobageDays: null })
  const ids = out.kept.map((k) => k.posting.posting_id)

  test("a German working-student posting is dropped", () => {
    expect(ids).not.toContain("2246042")
  })
  test("a Swiss one is kept", () => {
    expect(ids).toContain("2246044")
  })
  test("one with no resolvable country is kept AND flagged, never guessed at", () => {
    expect(ids).toContain("2246043")
    const unknown = out.kept.find((k) => k.posting.posting_id === "2246043")!
    expect(unknown.flags).toContain("conditional-country-unknown")
  })
})

describe("jobage", () => {
  test("an old posting is dropped, an undated one is kept and flagged", () => {
    const out = applyFilters(
      [posting({ posting_id: "old", date: "2026-01-01" }),
       posting({ posting_id: "undated", date: null })],
      CFG,
      { jobageDays: 30, today: "2026-08-18" },
    )
    const ids = out.kept.map((k) => k.posting.posting_id)
    expect(ids).toEqual(["undated"])
    expect(out.kept[0].flags).toContain("no-posting-date")
    expect(out.dropped.jobage).toBe(1)
  })
})

test("a broken conditional_exclude pattern does not take the run down", () => {
  const out = applyFilters([posting()], { ...CFG, conditional_exclude: [{ pattern: "([unclosed" }] })
  expect(out.kept.length).toBe(1)
})

describe("a macro-region only qualifies a remote posting", () => {
  test('an ONSITE role located "EMEA" is uncertain, not qualifying', () => {
    // "EMEA" on an onsite role names a market, not a place you could commute to,
    // so it neither qualifies nor counts as a geography we resolved: the posting
    // is surfaced and flagged for you rather than accepted or dropped.
    const out = applyFilters([posting({ locations: at("EMEA"), workplace: "onsite" })], CFG)
    expect(out.kept.length).toBe(1)
    expect(out.kept[0].location_uncertain).toBe(true)
    expect(out.kept[0].flags).toContain("location-uncertain")
    expect(locationVerdict(out.kept[0].places, "onsite", CFG).qualifies).toBe(false)
  })

  test('a REMOTE role in a region we do not accept is dropped, not flagged', () => {
    const verdict = locationVerdict([normalizePlace("APAC")], "remote", CFG)
    expect(verdict.qualifies).toBe(false)
    expect(verdict.uncertain).toBe(false)
  })

  test("the same posting marked remote does qualify", () => {
    const verdict = locationVerdict([normalizePlace("EMEA")], "remote", CFG)
    expect(verdict.qualifies).toBe(true)
  })
})
