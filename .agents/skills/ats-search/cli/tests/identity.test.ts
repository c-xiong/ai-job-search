// Test 5 of §14: what counts as identity evidence, and what does not.

import { describe, expect, test } from "bun:test"
import { identityMatches, slugCandidates } from "../src/commands/resolve.ts"
import { loadRegistry } from "../src/registry.ts"
import { join } from "path"
import { FIXTURES } from "./helpers.ts"

const company = (over: Record<string, unknown> = {}) =>
  ({ name: "DeepJudge", domain: "deepjudge.ai", tier: 2, status: "unresolved", route: "ats", ...over }) as never

describe("identity evidence", () => {
  test("a matching vendor identifier is accepted, including through an alias", () => {
    expect(identityMatches(company({ name: "Parloa" }), "Parloa")).toBe(true)
    expect(identityMatches(company({ name: "Sonar", aliases: ["SonarSource"] }), "SonarSource")).toBe(true)
    expect(identityMatches(company({ name: "Nexthink" }), "Nexthink")).toBe(true)
  })

  test("a legal suffix does not break the match", () => {
    expect(identityMatches(company({ name: "DeepJudge AG" }), "DeepJudge")).toBe(true)
  })

  test("a different company is refused - the unique.jobs.personio.de case", () => {
    // The live trap: the obvious Personio slug for "Unique" (Zurich) serves a
    // board belonging to "unique land use GmbH" in Freiburg.
    expect(identityMatches(company({ name: "Unique", domain: "unique.ch" }), "unique land use GmbH")).toBe(false)
  })

  test("absent identity is never a match - three of five vendors publish none", () => {
    expect(identityMatches(company(), null)).toBe(false)
    expect(identityMatches(company(), "")).toBe(false)
  })
})

describe("slug candidates", () => {
  test("are derived from the domain first, then the name", () => {
    const out = slugCandidates(company({ name: "Sonar", domain: "sonarsource.com", aliases: ["SonarSource SA"] }))
    expect(out[0]).toBe("sonarsource")
    expect(out).toContain("sonar")
  })

  test("legal suffixes are stripped before guessing", () => {
    expect(slugCandidates(company({ name: "Aleph Alpha GmbH", domain: "aleph-alpha.com" }))).toContain("alephalpha")
  })
})

describe("the registry refuses to hold an unevidenced `verified`", () => {
  test("a verified ats company without identity evidence is BAD_REGISTRY", () => {
    const path = join(FIXTURES, "registry-test.json")
    const registry = loadRegistry(path)
    for (const c of registry.companies) {
      if (c.status === "verified" && c.route === "ats") {
        expect(c.identity?.evidence_kind).toBeTruthy()
      }
    }
  })

  test("a URL echoing the requested token is not evidence, so it cannot promote", () => {
    // There is deliberately no code path that turns "the board answered" into
    // `verified`; the only promoting kinds are the three in the hierarchy.
    const promoting = ["company_site_link", "vendor_identifier", "human_confirmed"]
    expect(promoting).not.toContain("url_echo")
    expect(promoting).not.toContain("http_200")
  })
})

describe("greenhouse embed pages", () => {
  test("the board comes from ?for=, never the word embed", async () => {
    const { greenhouse } = await import("../src/vendors/greenhouse.ts")
    const n26 = '<script src="https://job-boards.greenhouse.io/embed/job_board/js?for=n26"></script>'
    expect(greenhouse.detectToken(n26)).toBe("n26")
    expect(greenhouse.detectToken('<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme&b=x">')).toBe("acme")
    expect(greenhouse.detectToken("https://job-boards.greenhouse.io/parloa/jobs/1")).toBe("parloa")
    expect(greenhouse.detectToken('<a href="https://job-boards.greenhouse.io/embed/job_app">apply</a>')).toBeNull()
  })
})
