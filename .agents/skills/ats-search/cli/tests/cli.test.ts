// Tests 11, 12, 13 and 16 of §14, plus the contract behaviors that only show up
// when the whole CLI runs: exit codes, cadence selection, the cooldown, and the
// promise that `-q` costs nothing.

import { describe, expect, test } from "bun:test"
import { writeFileSync, readFileSync, existsSync } from "fs"
import { join } from "path"
import { newSession, parseJSON, runCLI } from "./helpers.ts"
import type { SearchPayload } from "../src/types.ts"

describe("test 11 - partial failure never discards good results", () => {
  test("one schema_changed + one ok exits 0, reports degraded, keeps the good rows", async () => {
    const res = await runCLI(["search", "-c", "Parloa", "-c", "BrokenCo", "--no-write"])
    expect(res.exitCode).toBe(0)
    const payload = parseJSON<SearchPayload>(res)
    expect(payload.meta.degraded).toBe(true)
    expect(payload.results.length).toBeGreaterThan(0)
    expect(payload.results.every((r) => r.id.startsWith("greenhouse:parloa:"))).toBe(true)
    const broken = payload.meta.companies.find((c) => c.name === "BrokenCo")!
    expect(broken.status).toBe("schema_changed")
    expect(broken.message).toBeTruthy()
    expect(payload.meta.status_counts).toMatchObject({ ok: 1, schema_changed: 1 })
  })

  test("a degraded company keeps its old last_success_at so it is retried first", async () => {
    const session = newSession()
    await runCLI(["search", "-c", "Parloa", "-c", "DownCo"], {}, session)
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    const parloa = registry.companies.find((c: { name: string }) => c.name === "Parloa")
    const down = registry.companies.find((c: { name: string }) => c.name === "DownCo")
    expect(parloa.last_success_at).toBeTruthy()
    expect(down.last_success_at).toBeNull()
    // The attempt is still recorded - "we tried and it was down" is information.
    expect(down.last_attempt_at).toBeTruthy()
    expect(down.last_status).toBe("unavailable")
  })
})

describe("test 12 - hard failures", () => {
  test("every selected company failing exits 1 with ALL_COMPANIES_FAILED", async () => {
    const res = await runCLI(["search", "-c", "BrokenCo", "-c", "DownCo", "--no-write"])
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("ALL_COMPANIES_FAILED")
    // meta.companies is still on stdout, so the caller can see why without re-running.
    const payload = parseJSON<SearchPayload>(res)
    expect(payload.meta.companies.map((c) => c.status).sort()).toEqual(["schema_changed", "unavailable"])
  })

  test("a missing registry is BAD_REGISTRY", async () => {
    const res = await runCLI(["search"], { ATS_REGISTRY: "/nonexistent/companies.json" })
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("BAD_REGISTRY")
  })

  test("an unparseable registry is BAD_REGISTRY", async () => {
    const session = newSession()
    writeFileSync(session.registryPath, "{ not json", "utf-8")
    const res = await runCLI(["search"], {}, session)
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("BAD_REGISTRY")
  })

  test("a verified ats company with no identity evidence is BAD_REGISTRY", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    registry.companies[0].identity = null
    writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const res = await runCLI(["search"], {}, session)
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("BAD_REGISTRY")
    expect(JSON.parse(res.stderr).error).toContain("a working slug is not evidence")
  })

  test("an unknown --company is BAD_ARGS, not a silent empty run", async () => {
    const res = await runCLI(["search", "-c", "DefinitelyNotInTheRegistry"])
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("BAD_ARGS")
  })

  test("a bad --format is rejected before any request is made", async () => {
    const res = await runCLI(["search", "--format", "yaml"])
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("BAD_ARGS")
    expect(res.requests.length).toBe(0)
  })
})

describe("--company selector precedence", () => {
  test("a value that is both a company name and another entry's token resolves to the name", async () => {
    // PausedCo in the fixture registry carries the token "parloa". Matching both
    // would fetch the same board twice and double-count the run.
    const res = await runCLI(["search", "-c", "Parloa", "--no-write"])
    expect(res.requests.length).toBe(1)
    const payload = parseJSON<SearchPayload>(res)
    expect(payload.meta.companies.map((c) => c.name)).toEqual(["Parloa"])
  })
})

describe("test 13 - composite ids", () => {
  test("a composite id from search round-trips through detail", async () => {
    const search = await runCLI(["search", "-c", "DeepJudge", "--no-write"])
    const payload = parseJSON<SearchPayload>(search)
    const id = payload.results[0].id
    expect(id).toMatch(/^ashby:deepjudge:/)
    const detail = await runCLI(["detail", id])
    expect(detail.exitCode).toBe(0)
    expect(parseJSON<{ id: string }>(detail).id).toBe(id)
  })

  test("a posting URL is accepted too", async () => {
    const detail = await runCLI([
      "detail",
      "https://jobs.lever.co/sonarsource/54743786-1ad7-4be7-bcbe-67f660e9c381",
    ])
    expect(detail.exitCode).toBe(0)
    expect(parseJSON<{ title: string }>(detail).title).toBe("Graduate Software Engineer")
  })

  test("a bare posting id is rejected with AMBIGUOUS_ID, never guessed at", async () => {
    const res = await runCLI(["detail", "4635890101"])
    expect(res.exitCode).toBe(1)
    const err = JSON.parse(res.stderr)
    expect(err.code).toBe("AMBIGUOUS_ID")
    expect(err.error).toContain("<vendor>:<token>:<posting_id>")
    expect(res.requests.length).toBe(0)
  })
})

describe("test 16 - -q is local and free", () => {
  test("filters results without issuing a single extra request", async () => {
    const all = await runCLI(["search", "-c", "DeepJudge", "--no-write"])
    const filtered = await runCLI(["search", "-c", "DeepJudge", "-q", "research", "--no-write"])
    expect(all.requests.length).toBe(1)
    expect(filtered.requests.length).toBe(1)
    const a = parseJSON<SearchPayload>(all)
    const f = parseJSON<SearchPayload>(filtered)
    expect(f.results.length).toBeLessThan(a.results.length)
    expect(f.results.every((r) => /research/i.test(r.title))).toBe(true)
  })
})

describe("one batch call per run", () => {
  test("a whole run over five boards is five requests, not one per query term", async () => {
    const res = await runCLI(
      ["search", "-c", "Parloa", "-c", "DeepJudge", "-c", "Merantix", "-c", "Sonar", "--no-write"],
    )
    expect(res.exitCode).toBe(0)
    expect(res.requests.length).toBe(4)
  })

  test("vendors that ship descriptions inline trigger no detail request", async () => {
    const res = await runCLI(["search", "-c", "Sonar", "--no-write"])
    expect(res.requests.length).toBe(1)
    const payload = parseJSON<SearchPayload>(res)
    expect(payload.results[0].description).toBeTruthy()
  })

  test("--page 2 returns an empty set with a note, not an error", async () => {
    const res = await runCLI(["search", "--page", "2", "--no-write"])
    expect(res.exitCode).toBe(0)
    const payload = parseJSON<SearchPayload>(res)
    expect(payload.results).toEqual([])
    expect(payload.meta.note).toContain("page 1")
    expect(res.requests.length).toBe(0)
  })
})

describe("cadence selection", () => {
  test("companies --due picks only eligible companies, staleness first then tier", async () => {
    const res = await runCLI(["companies", "--due", "--format", "json", "--max-companies", "20"])
    const payload = parseJSON<{ results: Array<{ name: string; route: string; status: string }> }>(res)
    const names = payload.results.map((r) => r.name)
    // route linkedin, and cadence_days 0, are never auto-selected.
    expect(names).not.toContain("LinkedInOnlyCo")
    expect(names).not.toContain("PausedCo")
    expect(payload.results.every((r) => r.route === "ats" && r.status === "verified")).toBe(true)
  })

  test("--max-companies bounds the run and the rest keep their place in the queue", async () => {
    const res = await runCLI(["search", "--max-companies", "2", "--no-write"])
    const payload = parseJSON<SearchPayload>(res)
    expect(payload.meta.companies.length).toBe(2)
    expect(payload.meta.note).toContain("past --max-companies")
  })

  test("a company fetched successfully is not due again inside its cadence", async () => {
    const session = newSession()
    const first = await runCLI(["search", "-c", "Parloa"], {}, session)
    expect(first.exitCode).toBe(0)
    const due = await runCLI(["companies", "--due", "--format", "json", "--max-companies", "20"], {}, session)
    const names = parseJSON<{ results: Array<{ name: string }> }>(due).results.map((r) => r.name)
    expect(names).not.toContain("Parloa")
  })
})

describe("the per-company cooldown", () => {
  test("a second run inside min_interval_minutes serves cache and issues no request", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    registry.defaults.min_interval_minutes = 60
    writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")

    const first = await runCLI(["search", "-c", "Parloa"], {}, session)
    expect(first.requests.length).toBe(1)
    const second = await runCLI(["search", "-c", "Parloa", "--no-write"], {}, session)
    expect(second.requests.length).toBe(0)
    const payload = parseJSON<SearchPayload>(second)
    expect(payload.meta.companies[0].cached).toBe(true)
    expect(payload.results.length).toBe(parseJSON<SearchPayload>(first).results.length)
  })
  test("no-write does not create a board payload cache or advance registry bookkeeping", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    registry.companies = [registry.companies.find((c: { name: string }) => c.name === "Parloa")]
    registry.companies[0].name = "Example Employer"
    writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const before = readFileSync(session.registryPath, "utf-8")
    const result = await runCLI(["search", "-c", "Example Employer", "--no-write"], {}, session)
    expect(result.exitCode).toBe(0)
    expect(result.requests.length).toBe(1)
    expect(existsSync(join(session.dir, "cache", "greenhouse-parloa.json"))).toBe(false)
    expect(readFileSync(session.registryPath, "utf-8")).toBe(before)
  })
})

describe("the vendor access gate", () => {
  test("a vendor disabled by its published policy is not fetched, even when named explicitly", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    registry.defaults.vendor_access = {
      smartrecruiters: { enabled: false, robots: "disallow", why: "robots.txt disallows all but LinkedInBot" },
    }
    writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const res = await runCLI(["search", "-c", "Nexthink", "--no-write"], {}, session)
    // Refusing to ask is not the same as asking and finding nothing, so this is
    // a structured non-zero error rather than an empty success.
    expect(res.exitCode).toBe(1)
    expect(res.requests.length).toBe(0)
    const err = JSON.parse(res.stderr)
    expect(err.code).toBe("NO_FETCHABLE_COMPANY")
    expect(err.error).toContain("smartrecruiters is disabled")
  })

  test("a vendor whose published policy said no is off even with no vendor_access block", async () => {
    // Fail-closed: the policy lives in the CLI, not only in a registry block a
    // fork user can copy without.
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    delete registry.defaults.vendor_access
    writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const res = await runCLI(["search", "-c", "Nexthink", "--no-write"], {}, session)
    expect(res.exitCode).toBe(1)
    expect(res.requests.length).toBe(0)
    expect(JSON.parse(res.stderr).error).toContain("robots.txt")
  })

  test("`detail` is gated too - it reaches the same host `search` does", async () => {
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    delete registry.defaults.vendor_access
    writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    const res = await runCLI(["detail", "smartrecruiters:nexthink:744000144073601"], {}, session)
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("VENDOR_DISABLED")
    expect(res.requests.length).toBe(0)
  })
})

describe("explicit selection does not lift the rules", () => {
  test("an unverified company is refused, with the fix named", async () => {
    const res = await runCLI(["search", "-c", "UnverifiedCo", "--no-write"])
    expect(res.exitCode).toBe(1)
    const err = JSON.parse(res.stderr)
    expect(err.code).toBe("NO_FETCHABLE_COMPANY")
    expect(err.error).toContain("status ambiguous")
    expect(err.error).toContain("resolve")
    expect(res.requests.length).toBe(0)
  })

  test("--max-companies still bounds an explicit fan-out", async () => {
    const res = await runCLI(
      ["search", "-c", "Parloa", "-c", "DeepJudge", "-c", "Sonar", "--max-companies", "1", "--no-write"],
    )
    expect(res.exitCode).toBe(0)
    expect(res.requests.length).toBe(1)
    const payload = parseJSON<SearchPayload>(res)
    expect(payload.meta.companies.length).toBe(1)
    expect(payload.meta.note).toContain("past --max-companies")
  })

  test("a route: linkedin company is refused rather than silently skipped", async () => {
    const res = await runCLI(["search", "-c", "LinkedInOnlyCo", "--no-write"])
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("NO_FETCHABLE_COMPANY")
  })
})

describe("flag validation", () => {
  test("an unknown flag is BAD_ARGS, not a shrug", async () => {
    const res = await runCLI(["companies", "--wat"])
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("BAD_ARGS")
    expect(JSON.parse(res.stderr).error).toContain("--wat")
  })

  test("a misspelled bound does not silently fall back to the default", async () => {
    const res = await runCLI(["search", "--max-compaines", "1", "--no-write"])
    expect(res.exitCode).toBe(1)
    expect(res.requests.length).toBe(0)
  })

  test("a non-numeric bound is rejected instead of parseInt-ed", async () => {
    for (const value of ["8companies", "1e3", "-2", ""]) {
      const res = await runCLI(["search", "--max-companies", value, "--no-write"])
      expect(res.exitCode).toBe(1)
      expect(JSON.parse(res.stderr).code).toBe("BAD_ARGS")
    }
  })
})

describe("output shape", () => {
  test("every result carries the six contract fields plus a scored prefit", async () => {
    const res = await runCLI(["search", "-c", "Parloa", "--no-write"])
    const payload = parseJSON<SearchPayload>(res)
    for (const r of payload.results) {
      for (const key of ["id", "title", "company", "location", "date", "url"] as const) {
        expect(r[key]).toBeDefined()
      }
      expect(typeof r.prefit_score).toBe("number")
      expect(Array.isArray(r.prefit_reasons)).toBe(true)
      expect(r.prefit_reasons.length).toBeGreaterThan(0)
    }
  })

  test("the registry write touches only bookkeeping, never the fields you own", async () => {
    const session = newSession()
    const before = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    const result = await runCLI(["search", "-c", "Parloa"], {}, session)
    const report = parseJSON<SearchPayload>(result).meta.companies[0]
    const after = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    const b = before.companies.find((c: { name: string }) => c.name === "Parloa")
    const a = after.companies.find((c: { name: string }) => c.name === "Parloa")
    for (const key of ["name", "domain", "tier", "vendor", "token", "status", "route", "identity", "flags"]) {
      expect(a[key]).toEqual(b[key])
    }
    expect(after.defaults).toEqual(before.defaults)
    expect(a.stats.jobs_seen).toBeGreaterThan(0)
    expect(a.stats.last_jobs_seen).toBe(report.jobs_seen)
    expect(a.stats.last_eligible_jobs).toBe(report.eligible)
    const backup = JSON.parse(readFileSync(session.registryPath.replace(/\.json$/, ".backup.json"), "utf-8"))
    expect(backup).toEqual(before)
  })
})

describe("--max-new-jobs stops selecting, it does not truncate", () => {
  test("the company in flight is finished in full and the rest are left untouched", async () => {
    const session = newSession()
    const res = await runCLI(
      ["search", "-c", "Parloa", "-c", "DeepJudge", "-c", "Sonar", "--max-new-jobs", "1"],
      {},
      session,
    )
    expect(res.exitCode).toBe(0)
    const payload = parseJSON<SearchPayload>(res)
    // The first company was processed whole - more than the cap - and no second
    // company was fetched.
    expect(payload.meta.companies.length).toBe(1)
    expect(payload.results.length).toBeGreaterThan(1)
    expect(res.requests.length).toBe(1)
    expect(payload.meta.note).toContain("were not reached")
    expect(payload.meta.new_rows).toBeGreaterThan(0)

    // An unreached company must be indistinguishable from "never attempted", so
    // that the next run picks it first.
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    const unreached = registry.companies.filter((c: { name: string }) =>
      ["DeepJudge", "Sonar"].includes(c.name),
    )
    for (const c of unreached) {
      expect(c.last_attempt_at).toBeNull()
      expect(c.last_success_at).toBeNull()
      expect(c.stats.jobs_seen).toBe(0)
    }
  })

  test("0 means unbounded", async () => {
    const res = await runCLI(
      ["search", "-c", "Parloa", "-c", "DeepJudge", "--max-new-jobs", "0", "--no-write"],
    )
    expect(parseJSON<SearchPayload>(res).meta.companies.length).toBe(2)
  })

  test("rows already in the board do not count toward the bound", async () => {
    // The whole point of --known-ids. Without it, a first company whose entire
    // board you have already seen eats the budget and defers the company that
    // actually had new work.
    const session = newSession()
    const all = await runCLI(["search", "-c", "Parloa", "--no-write"], {}, session)
    const known = parseJSON<SearchPayload>(all).results.map((r) => r.id)
    expect(known.length).toBeGreaterThan(1)

    const knownFile = join(session.dir, "known.json")
    writeFileSync(knownFile, JSON.stringify(known), "utf-8")
    const res = await runCLI(
      ["search", "-c", "Parloa", "-c", "DeepJudge", "--max-new-jobs", "1",
       "--known-ids", knownFile, "--no-write"],
      {},
      session,
    )
    const payload = parseJSON<SearchPayload>(res)
    // Parloa contributed no NEW rows, so the budget was untouched and DeepJudge
    // was still reached.
    expect(payload.meta.companies.map((c) => c.name).sort()).toEqual(["DeepJudge", "Parloa"])
    expect(payload.meta.new_rows).toBeGreaterThan(0)
  })

  test("a --known-ids file that does not exist is BAD_ARGS, not a silent empty set", async () => {
    const res = await runCLI(["search", "-c", "Parloa", "--known-ids", "/nope/known.json", "--no-write"])
    expect(res.exitCode).toBe(1)
    expect(JSON.parse(res.stderr).code).toBe("BAD_ARGS")
    expect(res.requests.length).toBe(0)
  })
})

describe("a 429 stops the host", () => {
  test("no retry, and no further company on that host is asked in the same run", async () => {
    const session = newSession()
    const res = await runCLI(["search", "-c", "ThrottledCo", "-c", "Parloa", "--no-write"], {}, session)
    // One request total: the 429 is not retried, and Parloa lives on the same
    // host so it is not asked at all.
    expect(res.requests.length).toBe(1)
    const payload = parseJSON<SearchPayload>(res)
    const statuses = Object.fromEntries(payload.meta.companies.map((c) => [c.name, c.status]))
    expect(statuses.ThrottledCo).toBe("rate_limited")
    expect(statuses.Parloa).toBe("rate_limited")
  })

  test("the stop survives the process - a later run makes no request either", async () => {
    const session = newSession()
    await runCLI(["search", "-c", "ThrottledCo", "--no-write"], {}, session)
    const second = await runCLI(["search", "-c", "Parloa", "--no-write"], {}, session)
    expect(second.requests.length).toBe(0)
    expect(parseJSON<SearchPayload>(second).meta.companies[0].status).toBe("rate_limited")
  })

  test("a rate-limited company keeps its old last_success_at", async () => {
    const session = newSession()
    await runCLI(["search", "-c", "ThrottledCo"], {}, session)
    const company = JSON.parse(readFileSync(session.registryPath, "utf-8")).companies.find(
      (c: { name: string }) => c.name === "ThrottledCo",
    )
    expect(company.last_success_at).toBeNull()
    expect(company.last_status).toBe("rate_limited")
  })
})

describe("large output", () => {
  test("a payload over 64 KiB reaches the pipe whole", async () => {
    // Bun drops unflushed pipe output on process.exit(); the board then saw the
    // first 65,536 bytes of a search result and discarded the whole run.
    const session = newSession()
    const registry = JSON.parse(readFileSync(session.registryPath, "utf-8"))
    const template = registry.companies[0]
    for (let i = 0; i < 900; i++) {
      registry.companies.push({ ...template, name: `Bulk Company ${i}`, token: `bulk${i}`, aliases: [] })
    }
    writeFileSync(session.registryPath, JSON.stringify(registry), "utf-8")
    // Through an OS pipe, as Python's subprocess reads it: Bun.spawn's own pipe
    // drains before exit and would hide the bug.
    const cli = join(import.meta.dir, "../src/cli.ts")
    const proc = Bun.spawn(["sh", "-c", `bun run "${cli}" companies --format json | cat`], {
      stdout: "pipe",
      env: { ...process.env, ATS_FIXTURES: join(import.meta.dir, "fixtures"), ATS_REGISTRY: session.registryPath,
             ATS_CACHE_DIR: join(session.dir, "cache"), ATS_NOW: "2026-08-18T09:00:00.000Z" },
    })
    const stdout = await new Response(proc.stdout).text()
    await proc.exited
    expect(stdout.length).toBeGreaterThan(70000)
    expect(() => JSON.parse(stdout)).not.toThrow()
  })
})
