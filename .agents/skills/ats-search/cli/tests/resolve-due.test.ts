// The retry queue (COMPANIES_PLAN §3.2-3.3): detection is retried with backoff,
// handed to the owner after four misses, and a board that keeps 404-ing comes back.

import { describe, expect, test } from "bun:test"
import { readFileSync, writeFileSync } from "fs"
import { newSession, parseJSON, runCLI, type Session } from "./helpers.ts"
import {
  loadRegistry,
  resolveBookkeeping,
  selectResolveDue,
  writeBookkeeping,
  type Company,
} from "../src/registry.ts"

const company = (path: string, name: string) =>
  JSON.parse(readFileSync(path, "utf-8")).companies.find((c: { name: string }) => c.name === name)

function edit(session: Session, name: string, patch: Record<string, unknown>) {
  const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
  Object.assign(registry.companies.find((c: { name: string }) => c.name === name), patch)
  writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
}

async function due(session: Session) {
  const res = await runCLI(["resolve", "--due", "--max-companies", "50", "--max-probes", "25", "--format", "json"], {}, session)
  return parseJSON<{ results: Array<{ name: string; status: string }> }>(res).results.map((r) => r.name)
}

describe("resolve --due", () => {
  test("a miss is retried after a day, not again the same day", async () => {
    const session = newSession()
    await runCLI(["resolve", "-c", "ResolveNone", "--max-probes", "25", "--format", "json"], {}, session)
    const row = company(session.registryPath, "ResolveNone")
    expect(row.resolve_attempts).toBe(1)
    expect(row.last_resolve_at).toBe("2026-08-18")
    expect(row.next_resolve_at).toBe("2026-08-19")
    expect(await due(session)).not.toContain("ResolveNone")
  })

  test("the fourth miss stops the retries and asks the owner", async () => {
    const session = newSession()
    edit(session, "ResolveNone", { resolve_attempts: 3, next_resolve_at: "2026-08-10" })
    expect(await due(session)).toContain("ResolveNone")
    const row = company(session.registryPath, "ResolveNone")
    expect(row.resolve_attempts).toBe(4)
    expect(row.next_resolve_at).toBeNull()
    expect(await due(session)).not.toContain("ResolveNone")
  })

  test("a success clears the retry state", async () => {
    const session = newSession()
    edit(session, "ResolveAshby", { resolve_attempts: 2, next_resolve_at: "2026-08-10" })
    await due(session)
    const row = company(session.registryPath, "ResolveAshby")
    expect(row.status).toBe("verified")
    expect(row.resolve_attempts).toBe(0)
    expect("next_resolve_at" in row).toBe(false)
  })
})

describe("the queue's rules", () => {
  const base: Company = { name: "X", tier: 3, status: "unresolved", route: "ats" }
  const registry = (companies: Company[]) => ({ ...loadRegistry(newSession().registryPath), companies })

  test("ambiguous always waits for the owner", () => {
    expect(resolveBookkeeping(base, "ambiguous", "2026-08-18").next_resolve_at).toBeNull()
  })

  test("only a board that keeps answering not_found goes back to detection", () => {
    const picked = selectResolveDue(registry([
      { ...base, name: "Gone", status: "verified", missing_streak: 3 },
      { ...base, name: "Flaky", status: "verified", missing_streak: 2 },
      { ...base, name: "Asked", next_resolve_at: null },
      { ...base, name: "Later", next_resolve_at: "2026-09-01" },
      { ...base, name: "Linkedin", route: "linkedin" },
      { ...base, name: "Fresh" },
    ]), "2026-08-18", 10).map((c) => c.name)
    expect(picked.sort()).toEqual(["Fresh", "Gone"])
  })

  test("bookkeeping counts consecutive not_found and nothing else", () => {
    const session = newSession()
    const name = loadRegistry(session.registryPath).companies.find((c) => c.status === "verified")!.name
    const entry = (status: string, success: boolean) => ({ name, attempted_at: "2026-08-18T09:00:00Z", status, success, jobs_seen: 0, eligible: 0 })
    writeBookkeeping([entry("not_found", false)], session.registryPath)
    writeBookkeeping([entry("rate_limited", false)], session.registryPath)
    writeBookkeeping([entry("not_found", false)], session.registryPath)
    expect(company(session.registryPath, name).missing_streak).toBe(2)
    writeBookkeeping([entry("ok", true)], session.registryPath)
    expect(company(session.registryPath, name).missing_streak).toBe(0)
  })
})
