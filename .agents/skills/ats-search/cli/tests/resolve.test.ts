// `resolve` end to end against fixture careers pages: the evidence hierarchy is
// only worth anything if the code actually refuses to promote without evidence.

import { describe, expect, test } from "bun:test"
import { readFileSync } from "fs"
import { newSession, parseJSON, runCLI } from "./helpers.ts"

interface ResolveReport {
  name: string
  status: string
  vendor: string | null
  token: string | null
  evidence_kind: string | null
  evidence: string | null
  candidates: Array<{ vendor: string; token: string; note?: string }>
}

interface ResolvePayload {
  meta: { resolved: number; status_counts: Record<string, number>; dry_run: boolean }
  results: ResolveReport[]
}

function companyIn(path: string, name: string) {
  const registry = JSON.parse(readFileSync(path, "utf-8"))
  return registry.companies.find((c: { name: string }) => c.name === name)
}

async function resolve(names: string[], extra: string[] = [], session = newSession()) {
  const args = ["resolve", "--format", "json", "--max-probes", "25"]
  for (const n of names) args.push("-c", n)
  const res = await runCLI([...args, ...extra], {}, session)
  return { res, payload: parseJSON<ResolvePayload>(res), session }
}

describe("evidence-first resolution", () => {
  test("a link on the company's own careers page promotes to verified", async () => {
    const { payload, session } = await resolve(["ResolveAshby"])
    const report = payload.results[0]
    expect(report.status).toBe("verified")
    expect(report.vendor).toBe("ashby")
    expect(report.token).toBe("deepjudge")
    expect(report.evidence_kind).toBe("company_site_link")
    expect(report.evidence).toContain("resolve-ashby.test/careers")

    const company = companyIn(session.registryPath, "ResolveAshby")
    expect(company.status).toBe("verified")
    expect(company.identity.evidence_kind).toBe("company_site_link")
    // Phase 0 is only done when every route:ats row carries an explicit cadence.
    expect(typeof company.cadence_days).toBe("number")
  })

  test("the exact careers URL is tried before guessed domain paths", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    const company = registry.companies.find((c: { name: string }) => c.name === "ResolveAshby")
    company.careers_url = "https://resolve-ashby.test/careers"
    company.domain = "wrong-domain.test"
    require("fs").writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")

    const { payload, res } = await resolve(["ResolveAshby"], [], session)
    expect(res.requests[0]).toBe("https://resolve-ashby.test/careers")
    expect(payload.results[0].status).toBe("verified")
    expect(res.requests.some((url) => url.includes("wrong-domain.test"))).toBe(false)
  })

  test("a board that says it belongs to someone else is ambiguous, never verified", async () => {
    const { payload } = await resolve(["ResolveConflict"])
    const report = payload.results[0]
    expect(report.status).toBe("ambiguous")
    expect(report.evidence_kind).toBeNull()
    expect(report.detail ?? "").toBeDefined()
    expect(report.candidates[0].note).toContain("Parloa")
  })

  test("a recognizable ATS with no adapter is unsupported_vendor, not silence", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    registry.companies.find((c: { name: string }) => c.name === "ResolveWorkday").domain = "resolve-teamtailor.test"
    require("fs").writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const { payload } = await resolve(["ResolveWorkday"], [], session)
    expect(payload.results[0].status).toBe("unsupported_vendor")
    // Monthly re-check, so a later adapter picks it up without the owner.
    expect(companyIn(session.registryPath, "ResolveWorkday").next_resolve_at).toBe("2026-09-17")
  })

  test("a board the careers page names but that no longer exists is not offered as a choice", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    registry.companies.find((c: { name: string }) => c.name === "ResolveWorkday").domain = "resolve-dead.test"
    require("fs").writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const { payload } = await resolve(["ResolveWorkday"], [], session)
    const report = payload.results[0]
    expect(report.status).not.toBe("ambiguous")
    expect(report.candidates.some((c) => c.token === "deadco")).toBe(false)
    expect((report as unknown as { detail: string }).detail).toContain("greenhouse:deadco, but that board no longer exists")
  })

  test("a Workday link on the careers page is verified through the listing API", async () => {
    const { payload, session, res } = await resolve(["ResolveWorkday"])
    const report = payload.results[0]
    expect(report.status).toBe("verified")
    expect(report.vendor).toBe("workday")
    expect(report.token).toBe("acme.wd3/External")
    expect(report.evidence_kind).toBe("company_site_link")
    expect(res.requests).toContain("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    expect(companyIn(session.registryPath, "ResolveWorkday").resolve_attempts).toBe(0)
  })

  test("failing to find anything is `unresolved` - never `no_public_board`", async () => {
    const { payload } = await resolve(["ResolveNone"])
    expect(payload.results[0].status).toBe("unresolved")
    expect(payload.results[0].status).not.toBe("no_public_board")
  })

  test("a slug guess is only promoted when the payload names the company", async () => {
    const { payload, session } = await resolve(["SlugCo"])
    const report = payload.results[0]
    expect(report.status).toBe("verified")
    expect(report.evidence_kind).toBe("vendor_identifier")
    expect(report.evidence).toContain("SlugCo")
    expect(companyIn(session.registryPath, "SlugCo").token).toBe("slugco")
  })

  test("a careers page naming a disabled vendor never reaches that host", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    delete registry.defaults.vendor_access
    require("fs").writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const { res, payload } = await resolve(["ResolveSr"], [], session)
    expect(payload.results[0].status).toBe("unsupported_vendor")
    expect(res.requests.some((u) => u.includes("api.smartrecruiters.com"))).toBe(false)
  })
})

describe("resolution write-back keeps the registry self-consistent", () => {
  test("a row that stops being verified loses the identity that no longer holds", async () => {
    const session = newSession()
    const before = companyIn(session.registryPath, "ResolveDemote")
    expect(before.status).toBe("verified")
    expect(before.identity).toBeTruthy()
    expect(before.candidates).toBeTruthy()

    await resolve(["ResolveDemote"], [], session)
    const after = companyIn(session.registryPath, "ResolveDemote")
    expect(after.status).not.toBe("verified")
    expect(after.identity).toBeNull()
  })

  test("a promoted row loses the candidate list that was only ever a question", async () => {
    const { session } = await resolve(["ResolveAshby"])
    expect(companyIn(session.registryPath, "ResolveAshby").candidates).toBeUndefined()
  })

  test("--dry-run writes nothing", async () => {
    const session = newSession()
    const before = readFileSync(session.registryPath, "utf-8")
    const { payload } = await resolve(["ResolveAshby"], ["--dry-run"], session)
    expect(payload.meta.dry_run).toBe(true)
    expect(payload.results[0].status).toBe("verified")
    expect(readFileSync(session.registryPath, "utf-8")).toBe(before)
  })
})
