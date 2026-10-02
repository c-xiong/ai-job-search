import { afterEach, describe, expect, test } from "bun:test";
import { htmlFetch } from "../src/helpers";

// Rate limits stop immediately; transient 5xx has a small bounded retry loop.
// loop offline: a stubbed fetch counts attempts, and a stubbed setTimeout
// fires immediately so the exhaustion case does not sleep through the real
// 500ms -> 8s backoff schedule.

const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
});

function instantTimers() {
  globalThis.setTimeout = ((fn: () => void) =>
    originalSetTimeout(fn, 0)) as unknown as typeof setTimeout;
}

function stubFetch(responses: Array<() => Response>): { calls: number } {
  const state = { calls: 0 };
  globalThis.fetch = (async () => {
    const i = Math.min(state.calls, responses.length - 1);
    state.calls++;
    return responses[i]();
  }) as unknown as typeof fetch;
  return state;
}

describe("htmlFetch retry/backoff", () => {
  test("stops a 429 without requesting the next response", async () => {
    instantTimers();
    const state = stubFetch([
      () => new Response("", { status: 429 }),
      () => new Response("<html>ok</html>", { status: 200 }),
    ]);

    await expect(htmlFetch("https://www.linkedin.com/x")).rejects.toThrow(/429/);
    expect(state.calls).toBe(1);
  });

  test("returns the documented empty string on 404 without retrying", async () => {
    const state = stubFetch([() => new Response("", { status: 404 })]);

    const html = await htmlFetch("https://www.linkedin.com/x");
    expect(html).toBe("");
    expect(state.calls).toBe(1);
  });

  test("gives up after the initial attempt plus two retries on persistent 5xx", async () => {
    instantTimers();
    const state = stubFetch([() => new Response("", { status: 500 })]);

    await expect(htmlFetch("https://www.linkedin.com/x")).rejects.toThrow(/500/);
    expect(state.calls).toBe(3);
  });
});
