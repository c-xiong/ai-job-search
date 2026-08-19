// Test 18 of §14: one live smoke test, skipped when offline.
//
// Everything else in this suite runs against dated fixtures. This is the one
// place that touches a real vendor, because a parser can be perfectly consistent
// with a fixture that stopped resembling the live payload months ago. It fetches
// exactly one board and asks only for what the contract guarantees.

import { expect, test } from "bun:test"
import { mkdirSync, mkdtempSync, writeFileSync } from "fs"
import { join } from "path"
import { runCLI, type Session } from "./helpers.ts"
import type { SearchPayload } from "../src/types.ts"

const ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/parloa/jobs"

async function online(): Promise<boolean> {
  try {
    const res = await fetch(ENDPOINT, {
      method: "HEAD",
      headers: { "User-Agent": "ats-search-cli/1.0" },
      signal: AbortSignal.timeout(8000),
    })
    return res.status < 500
  } catch {
    return false
  }
}

function liveSession(): Session {
  const tmpRoot = join(import.meta.dir, "..", ".tmp-tests")
  mkdirSync(tmpRoot, { recursive: true })
  const dir = mkdtempSync(join(tmpRoot, "live-"))
  const registryPath = join(dir, "companies.json")
  writeFileSync(
    registryPath,
    JSON.stringify({
      defaults: {
        allowed_countries: ["CH", "DE", "AT"],
        remote_regions: ["europe", "eu", "dach", "emea"],
        cities: ["Berlin", "Munich", "Zurich"],
        title_include: ["engineer", "developer", "software", "data", "ai", "ml"],
        title_exclude: ["sales", "recruiter"],
        conditional_exclude: [],
        cadence_days: { "3": 3 },
        min_interval_minutes: 0,
      },
      companies: [
        {
          name: "Parloa",
          domain: "parloa.com",
          tier: 3,
          vendor: "greenhouse",
          token: "parloa",
          status: "verified",
          route: "ats",
          identity: {
            evidence_kind: "vendor_identifier",
            evidence: "greenhouse board 'parloa' reports company_name 'Parloa'",
            verified_at: "2026-08-18",
          },
          last_attempt_at: null,
          last_success_at: null,
          last_status: null,
          stats: { jobs_seen: 0, german_gated: 0, eligible_jobs: 0, last_eligible_at: null },
          flags: [],
        },
      ],
    }),
    "utf-8",
  )
  return { dir, registryPath }
}

test("live smoke: one real board, one request, parseable results", async () => {
  if (!(await online())) {
    console.log("skipping live smoke test - boards-api.greenhouse.io is unreachable")
    return
  }
  // ATS_FIXTURES is deliberately cleared: this is the one test that must not be
  // answered from disk.
  const res = await runCLI(
    ["search", "--max-companies", "1", "--limit", "3", "--no-write"],
    { ATS_FIXTURES: "", ATS_NOW: "" },
    liveSession(),
  )
  expect(res.exitCode).toBe(0)
  const payload = JSON.parse(res.stdout) as SearchPayload
  expect(payload.meta.companies.length).toBe(1)
  expect(payload.meta.companies[0].status).toBe("ok")
  expect(payload.meta.requests).toBe(1)
  expect(payload.meta.companies[0].jobs_seen).toBeGreaterThan(0)
  for (const r of payload.results) {
    expect(r.id).toMatch(/^greenhouse:parloa:\d+$/)
    expect(r.title.length).toBeGreaterThan(0)
    expect(r.url.startsWith("http")).toBe(true)
    expect(r.date === null || /^\d{4}-\d{2}-\d{2}$/.test(r.date)).toBe(true)
  }
}, 45000)
