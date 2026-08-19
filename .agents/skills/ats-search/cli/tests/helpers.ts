import { mkdtempSync, copyFileSync, mkdirSync, readFileSync, existsSync, unlinkSync } from "fs"
import { join } from "path"

export const FIXTURES = join(import.meta.dir, "fixtures")
const CLI_PATH = join(import.meta.dir, "../src/cli.ts")

export interface CLIResult {
  stdout: string
  stderr: string
  exitCode: number
  /** Every URL the run requested, in order (via ATS_REQUEST_LOG). */
  requests: string[]
  /** The registry file the run actually used - a throwaway copy. */
  registryPath: string
}

/**
 * Run the CLI fully offline: fixtures instead of the network, a throwaway copy
 * of the fixture registry so bookkeeping writes cannot leak between tests, and
 * a frozen clock.
 */
export interface Session {
  dir: string
  registryPath: string
}

/**
 * A throwaway working directory: its own registry copy and its own cooldown
 * cache. Pass the same session to two runs to test what the *second* run does
 * with what the first one left behind.
 */
// Under the package, not os.tmpdir(): a sandboxed CI or review environment can
// have /tmp read-only, and a test suite that cannot run is a test suite that
// stops catching things. `.tmp-tests/` is gitignored.
const TMP_ROOT = join(import.meta.dir, "..", ".tmp-tests")

export function newSession(registryFixture = "registry-test.json"): Session {
  mkdirSync(TMP_ROOT, { recursive: true })
  const dir = mkdtempSync(join(TMP_ROOT, "session-"))
  const registryPath = join(dir, "companies.json")
  copyFileSync(join(FIXTURES, registryFixture), registryPath)
  return { dir, registryPath }
}

export async function runCLI(
  args: string[],
  env: Record<string, string> = {},
  session: Session = newSession(),
): Promise<CLIResult> {
  const { dir, registryPath } = session
  const requestLog = join(dir, "requests.log")

  const proc = Bun.spawn(["bun", "run", CLI_PATH, ...args], {
    stdout: "pipe",
    stderr: "pipe",
    env: {
      ...process.env,
      ATS_FIXTURES: FIXTURES,
      ATS_REGISTRY: registryPath,
      ATS_CACHE_DIR: join(dir, "cache"),
      ATS_REQUEST_LOG: requestLog,
      ATS_NOW: "2026-08-18T09:00:00.000Z",
      ATS_SR_PAGE_SIZE: "2",
      ...env,
    },
  })
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ])
  const requests = existsSync(requestLog)
    ? readFileSync(requestLog, "utf-8").split("\n").filter(Boolean)
    : []
  if (existsSync(requestLog)) unlinkSync(requestLog)
  return { stdout: stdout.trim(), stderr: stderr.trim(), exitCode, requests, registryPath }
}

export function parseJSON<T = unknown>(result: CLIResult): T {
  try {
    return JSON.parse(result.stdout) as T
  } catch {
    throw new Error(`stdout is not JSON.\nstdout: ${result.stdout}\nstderr: ${result.stderr}`)
  }
}

export function fixture(name: string): string {
  return readFileSync(join(FIXTURES, name), "utf-8")
}

/** A transport that answers from the fixture directory, for unit-level tests. */
export function fixtureGet() {
  const map = JSON.parse(fixture("map.json")) as Record<string, { file?: string; status?: number; body?: string }>
  const seen: string[] = []
  const get = async (url: string) => {
    seen.push(url)
    const hit = map[url]
    if (!hit) return { status: 404, body: "", url }
    return { status: hit.status ?? 200, body: hit.body ?? (hit.file ? fixture(hit.file) : ""), url }
  }
  return { get, seen }
}
