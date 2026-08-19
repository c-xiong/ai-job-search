// Tests 1-4, 7 and 14 of the plan's §14 table: every vendor parser maps its
// dated fixture to the contract shape, and every known trap is pinned.

import { beforeAll, describe, expect, test } from "bun:test"
import { fixture, fixtureGet } from "./helpers.ts"
import { greenhouse, parseBoard as parseGreenhouse } from "../src/vendors/greenhouse.ts"
import { ashby, parseBoard as parseAshby } from "../src/vendors/ashby.ts"
import { personio, parseBoard as parsePersonio } from "../src/vendors/personio.ts"
import { lever, parseBoard as parseLever } from "../src/vendors/lever.ts"
import { smartrecruiters } from "../src/vendors/smartrecruiters.ts"
import type { RawPosting } from "../src/types.ts"

// The SmartRecruiters fixture board has 3 postings; a page size of 2 is what makes
// the pagination path reachable without a 101-posting fixture.
beforeAll(() => {
  process.env.ATS_SR_PAGE_SIZE = "2"
})

/** Test 1: no required contract field may come back `undefined`. */
function assertContractShape(postings: RawPosting[]) {
  expect(postings.length).toBeGreaterThan(0)
  for (const p of postings) {
    for (const key of [
      "posting_id", "title", "company", "locations", "workplace",
      "date", "updated_at", "url", "description", "deadline", "listed",
    ] as const) {
      expect(p[key]).toBeDefined()
    }
    expect(typeof p.posting_id).toBe("string")
    expect(p.posting_id.length).toBeGreaterThan(0)
    expect(typeof p.title).toBe("string")
    expect(Array.isArray(p.locations)).toBe(true)
    for (const hint of p.locations) {
      expect(typeof hint.text).toBe("string")
      expect(hint.text.length).toBeGreaterThan(0)
      expect(hint.country === null || typeof hint.country === "string").toBe(true)
    }
    expect(p.date === null || /^\d{4}-\d{2}-\d{2}$/.test(p.date)).toBe(true)
    expect(p.url.startsWith("http")).toBe(true)
  }
}

describe("test 1 - every vendor parser maps its fixture to the contract shape", () => {
  test("greenhouse", () => {
    const { postings, identity } = parseGreenhouse(fixture("greenhouse-parloa-20260818.json"), "parloa")
    assertContractShape(postings)
    expect(identity).toBe("Parloa")
    expect(postings[0].deadline).toBe("2026-09-30")
    expect(postings[0].description).toContain("LLM")
  })

  test("ashby", () => {
    const { postings, identity } = parseAshby(fixture("ashby-deepjudge-20260818.json"), "deepjudge")
    assertContractShape(postings)
    // Ashby publishes no company identifier at all - that is a fact about the
    // vendor, and §8 depends on it being reported honestly rather than faked.
    expect(identity).toBeNull()
    expect(postings[0].locations[0]).toEqual({ text: "Zurich", country: "Switzerland" })
    expect(postings[0].description).toContain("RAG")
  })

  test("personio", () => {
    const { postings } = parsePersonio(fixture("personio-merantix-20260818.xml"), "merantix")
    assertContractShape(postings)
    // The <jobDescriptions> block carries its own <name> elements; the position's
    // title must survive that.
    expect(postings[0].title).toBe("Machine Learning Engineer (m/f/d)")
    expect(postings[0].company).toBe("Merantix Momentum")
    expect(postings[0].description).toContain("PyTorch")
    // CDATA + entities: "MSc &amp; strong Python" must decode.
    expect(postings[0].description).toContain("MSc & strong Python")
    expect(postings[3].locations).toEqual([{ text: "Zürich", country: null }])
  })

  test("lever", () => {
    const { postings } = parseLever(fixture("lever-sonarsource-20260818.json"), "sonarsource")
    assertContractShape(postings)
    // createdAt is epoch milliseconds, not an ISO string.
    expect(postings[0].date).toBe("2026-08-13")
    // The ISO-2 belongs to the primary location, not to every allLocations entry.
    expect(postings[0].locations[0]).toEqual({ text: "Geneva", country: "CH" })
    // The requirements live in `lists`, not in descriptionPlain.
    expect(postings[0].description).toContain("Docker")
  })

  test("smartrecruiters", async () => {
    const { get } = fixtureGet()
    const board = await smartrecruiters.fetchBoard("nexthink", get)
    expect(board.status).toBe("ok")
    assertContractShape(board.postings)
    expect(board.identity).toBe("Nexthink")
    expect(board.postings[0].locations[0].country).toBe("ch")
  })
})

test("test 2 - SmartRecruiters 200 + totalFound 0 is not_found, never ok/empty", async () => {
  const { get } = fixtureGet()
  const board = await smartrecruiters.fetchBoard("zzz-not-a-real-company-xyz", get)
  expect(board.status).toBe("not_found")
  expect(board.status).not.toBe("empty")
  expect(board.postings).toEqual([])
  expect(board.message).toContain("totalFound 0")
})

test("test 3 - Personio 307 is not_found", async () => {
  const { get } = fixtureGet()
  const board = await personio.fetchBoard("nosuchcompany", get)
  expect(board.status).toBe("not_found")
  expect(board.message).toContain("307")
})

test("test 4 - a truncated payload is schema_changed, not a throw and not an empty list", async () => {
  const { get } = fixtureGet()
  const board = await greenhouse.fetchBoard("brokenco", get)
  expect(board.status).toBe("schema_changed")
  expect(board.postings).toEqual([])
  expect(board.message).toBeTruthy()
})

test("test 4b - a 5xx is unavailable, and is never confused with an empty board", async () => {
  const { get } = fixtureGet()
  const board = await greenhouse.fetchBoard("downco", get)
  expect(board.status).toBe("unavailable")
})

describe("test 7 - dates are reported honestly", () => {
  test("greenhouse date comes from first_published, with updated_at kept separately", () => {
    const { postings } = parseGreenhouse(fixture("greenhouse-parloa-20260818.json"), "parloa")
    expect(postings[0].date).toBe("2026-08-12")
    expect(postings[0].updated_at).toBe("2026-08-15")
    expect(postings[0].date).not.toBe(postings[0].updated_at)
  })

  test("a missing vendor date is null and the posting is retained - never now()", () => {
    const { postings } = parseGreenhouse(fixture("greenhouse-parloa-20260818.json"), "parloa")
    const undated = postings.find((p) => p.posting_id === "4635890103")
    expect(undated).toBeDefined()
    expect(undated!.date).toBeNull()
    const today = new Date().toISOString().slice(0, 10)
    expect(undated!.date).not.toBe(today)
  })
})

describe("test 14 - SmartRecruiters pagination", () => {
  test("fetches page 2 and stops at totalFound", async () => {
    const { get, seen } = fixtureGet()
    const board = await smartrecruiters.fetchBoard("nexthink", get)
    expect(board.postings.length).toBe(3)
    expect(seen.length).toBe(2)
    expect(seen[1]).toContain("offset=2")
  })

  test("an incomplete pagination reports success_partial, never ok", async () => {
    const { get } = fixtureGet()
    const board = await smartrecruiters.fetchBoard("halfnexthink", get)
    expect(board.status).toBe("success_partial")
    expect(board.postings.length).toBe(2)
    expect(board.status).not.toBe("ok")
  })
})

test("an Ashby secondary location keeps its own country, not the primary's", () => {
  const { postings } = parseAshby(fixture("ashby-multi-country-20260818.json"), "multi")
  expect(postings[0].locations).toEqual([
    { text: "New York", country: "United States" },
    { text: "Berlin", country: "Germany" },
    { text: "Somewhere", country: null },
  ])
})

test("unlisted Ashby postings never reach the pipeline", () => {
  const { postings } = parseAshby(fixture("ashby-deepjudge-20260818.json"), "deepjudge")
  expect(postings.some((p) => p.title.includes("unlisted"))).toBe(false)
})

test("lever detail round-trips a single posting", async () => {
  const { get } = fixtureGet()
  const posting = await lever.fetchDetail("sonarsource", "54743786-1ad7-4be7-bcbe-67f660e9c381", get)
  expect(posting?.title).toBe("Graduate Software Engineer")
})
