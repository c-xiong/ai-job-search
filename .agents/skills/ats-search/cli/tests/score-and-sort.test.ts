// Tests 15 and 17 of §14: a total, deterministic sort, and a bounded prefit that
// never appears without its reasons.

import { describe, expect, test } from "bun:test"
import { prefit } from "../src/score.ts"
import { stableSort } from "../src/helpers.ts"
import { applyFilters, type FilterConfig } from "../src/filters.ts"
import type { JobResult, RawPosting } from "../src/types.ts"

const CFG: FilterConfig = {
  allowed_countries: ["CH", "DE", "AT"],
  remote_regions: ["europe", "eu"],
  cities: ["Zurich", "Berlin"],
  title_include: ["engineer", "ml", "machine learning", "nlp", "data", "research", "intern", "software"],
  title_exclude: ["sales"],
  conditional_exclude: [],
}

function at(...texts: string[]) {
  return texts.map((text) => ({ text, country: null }))
}

function item(over: Partial<RawPosting> = {}) {
  const posting: RawPosting = {
    posting_id: "1", title: "Machine Learning Engineer", company: null, locations: at("Zurich"),
    workplace: null, date: "2026-08-15", updated_at: null,
    url: "https://example.com/1", description: null, deadline: null, listed: true,
    ...over,
  }
  return applyFilters([posting], CFG).kept[0]
}

describe("test 17 - prefit", () => {
  test("is deterministic", () => {
    const a = prefit(item(), 1, "2026-08-18", { cities: CFG.cities })
    const b = prefit(item(), 1, "2026-08-18", { cities: CFG.cities })
    expect(a).toEqual(b)
  })

  test("is bounded 0-100 across every combination we can build", () => {
    for (const tier of [1, 2, 3, 4, 5]) {
      for (const title of ["Machine Learning Engineer", "Senior NLP Research Scientist", "Data Intern", "Engineer"]) {
        for (const date of ["2026-08-18", "2024-01-01", null]) {
          for (const locations of [at("Zurich"), at("Remote"), at("Munich")]) {
            const p = prefit(item({ title, date, locations }), tier, "2026-08-18", { cities: CFG.cities })
            expect(p.score).toBeGreaterThanOrEqual(0)
            expect(p.score).toBeLessThanOrEqual(100)
          }
        }
      }
    }
  })

  test("never produces a number without reasons", () => {
    const p = prefit(item({ title: "Engineer", locations: at("Remote"), date: null }), 5, "2026-08-18", { cities: [] })
    expect(p.reasons.length).toBeGreaterThan(0)
    expect(p.reasons.length).toBeLessThanOrEqual(3)
  })

  test("a missing date scores neutral, not zero", () => {
    const dated = prefit(item({ date: "2026-08-17" }), 2, "2026-08-18", { cities: CFG.cities })
    const undated = prefit(item({ date: null }), 2, "2026-08-18", { cities: CFG.cities })
    const ancient = prefit(item({ date: "2020-01-01" }), 2, "2026-08-18", { cities: CFG.cities })
    expect(undated.score).toBeLessThan(dated.score)
    expect(undated.score).toBeGreaterThan(ancient.score)
  })

  test("a target city outranks an allowed country outranks an unresolved location", () => {
    const city = prefit(item({ locations: at("Zurich") }), 2, "2026-08-18", { cities: CFG.cities })
    const cty = prefit(item({ locations: at("Basel") }), 2, "2026-08-18", { cities: CFG.cities })
    const unknown = prefit(item({ locations: at("Remote") }), 2, "2026-08-18", { cities: CFG.cities })
    expect(city.score).toBeGreaterThan(cty.score)
    expect(cty.score).toBeGreaterThan(unknown.score)
  })
})

describe("test 15 - the sort is total and deterministic", () => {
  const row = (over: Partial<JobResult>): JobResult =>
    ({
      id: "x", title: "t", company: null, location: null, date: null, url: "u",
      vendor: "ashby", token: "t", tier: 1, locations: [], city: null, country: null,
      region: null, workplace: "onsite", location_uncertain: false, updated_at: null,
      deadline: null, description: null, prefit_score: 0, prefit_reasons: [], flags: [],
      ...over,
    }) as JobResult

  const rows = [
    row({ id: "ashby:a:2", prefit_score: 50, date: "2026-08-01" }),
    row({ id: "ashby:a:1", prefit_score: 50, date: "2026-08-01" }),
    row({ id: "ashby:a:3", prefit_score: 50, date: null }),
    row({ id: "ashby:a:4", prefit_score: 80, date: "2026-01-01" }),
    row({ id: "ashby:a:5", prefit_score: 50, date: "2026-08-09" }),
  ]

  test("prefit desc, then date desc with nulls last, then id asc", () => {
    expect(stableSort(rows).map((r) => r.id)).toEqual([
      "ashby:a:4", "ashby:a:5", "ashby:a:1", "ashby:a:2", "ashby:a:3",
    ])
  })

  test("input order does not change the result, so --limit truncates reproducibly", () => {
    const shuffled = [...rows].reverse()
    expect(stableSort(shuffled).map((r) => r.id)).toEqual(stableSort(rows).map((r) => r.id))
    expect(stableSort(shuffled).slice(0, 2).map((r) => r.id)).toEqual(["ashby:a:4", "ashby:a:5"])
  })

  test("sorting does not mutate the input", () => {
    const before = rows.map((r) => r.id)
    stableSort(rows)
    expect(rows.map((r) => r.id)).toEqual(before)
  })
})
