// The Workday adapter against its recorded shape: parsing, the country facet,
// pagination and the cap - with an in-memory transport, no network.

import { describe, expect, test } from "bun:test"
import { readFileSync } from "fs"
import { join } from "path"
import type { FetchOpts, HttpResponse } from "../src/helpers.ts"
import { countryFacet, parsePage, parseToken, workday } from "../src/vendors/workday.ts"
import { FIXTURES } from "./helpers.ts"

const PAGE = readFileSync(join(FIXTURES, "workday-acme-20260928.json"), "utf-8")

function fake(answer: (body: { appliedFacets: Record<string, string[]>; offset: number }) => HttpResponse) {
  const calls: Array<{ url: string; opts?: FetchOpts }> = []
  const get = async (url: string, opts?: FetchOpts) => {
    calls.push({ url, opts })
    return answer(JSON.parse(opts?.body ?? "{}"))
  }
  return { get, calls }
}

describe("workday", () => {
  test("the token names tenant, pod and site; the board and API follow from it", () => {
    expect(parseToken("novartis.wd3/Novartis_Careers")).toEqual({ tenant: "novartis", pod: "wd3", site: "Novartis_Careers" })
    expect(parseToken("novartis")).toBeNull()
    expect(workday.boardUrl("acme.wd3/External")).toBe("https://acme.wd3.myworkdayjobs.com/External")
    expect(workday.endpoint("acme.wd3/External")).toBe("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    expect(workday.guessable).toBe(false)
  })

  test("detects both Workday URL shapes on a careers page", () => {
    expect(workday.detectToken('<a href="https://acme.wd3.myworkdayjobs.com/en-US/External">Jobs</a>')).toBe("acme.wd3/External")
    expect(workday.detectToken("https://wd5.myworkdaysite.com/recruiting/beta/Careers/job/x")).toBe("beta.wd5/Careers")
    expect(workday.detectToken("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External/jobs")).toBeNull()
  })

  test("a listing becomes postings; '3 Locations' is not a place and dates stay unknown", () => {
    const postings = parsePage(PAGE).jobPostings!.length
    expect(postings).toBe(2)
    const { get } = fake(() => ({ status: 200, body: PAGE, url: "" }))
    return workday.fetchBoard("acme.wd3/External", get).then((board) => {
      expect(board.status).toBe("ok")
      expect(board.postings[0].posting_id).toBe("R-1001")
      expect(board.postings[0].url).toBe("https://acme.wd3.myworkdayjobs.com/External/job/Zurich/Machine-Learning-Engineer_R-1001")
      expect(board.postings[0].locations[0].text).toBe("Zurich")
      expect(board.postings[1].locations).toEqual([])
      expect(board.postings.every((p) => p.date === null && p.description === null)).toBe(true)
    })
  })

  test("a country facet narrows the board to the owner's countries", async () => {
    expect(countryFacet(parsePage(PAGE).facets, ["CH", "DE"])).toEqual({ locationCountry: ["ch-id"] })
    const { get, calls } = fake(() => ({ status: 200, body: PAGE, url: "" }))
    await workday.fetchBoard("acme.wd3/External", get, { countries: ["CH"] })
    expect(calls.length).toBe(2)
    expect(JSON.parse(calls[1].opts!.body!).appliedFacets).toEqual({ locationCountry: ["ch-id"] })
    expect(calls.every((c) => c.opts?.method === "POST")).toBe(true)
  })

  test("pages newest-first and stops at the cap, saying so", async () => {
    const one = (offset: number) => ({
      title: `Job ${offset}`, externalPath: `/job/Zurich/Job_${offset}`, locationsText: "Zurich", bulletFields: [`R-${offset}`],
    })
    const { get, calls } = fake(({ offset }) => ({
      status: 200, url: "",
      body: JSON.stringify({ total: offset === 0 ? 5000 : 0, jobPostings: Array.from({ length: 20 }, (_, i) => one(offset + i)) }),
    }))
    const board = await workday.fetchBoard("acme.wd3/External", get)
    expect(calls.length).toBe(25)
    expect(board.postings.length).toBe(500)
    expect(board.status).toBe("ok")
    expect(board.message).toContain("newest 500 of 5000")
  })

  test("an unknown tenant or site (Workday's 422) is not_found, not a schema change", async () => {
    const { get } = fake(() => ({ status: 422, body: '{"errorCode":"HTTP_422"}', url: "" }))
    expect((await workday.fetchBoard("nope.wd3/Site", get)).status).toBe("not_found")
  })
})
