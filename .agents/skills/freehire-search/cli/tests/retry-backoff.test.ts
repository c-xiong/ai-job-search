import { afterEach, describe, expect, test } from "bun:test";
import { apiGet } from "../src/helpers";

// Rate limits stop immediately; transient 5xx has a small bounded retry loop.
// loop offline: a stubbed fetch counts attempts, and a stubbed setTimeout
// fires immediately so the exhaustion case does not sleep through the real
// 500ms -> 8s backoff schedule. apiGet's documented graceful-degradation
// contract (connection failures fail fast, no retry) is pinned too.

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

describe("apiGet retry/backoff", () => {
  test("stops on a 429 without requesting another response", async () => {
    instantTimers();
    const state = stubFetch([
      () => new Response("", { status: 429 }),
      () => new Response('{"data":[]}', { status: 200 }),
    ]);

    await expect(apiGet<unknown[]>("/x")).rejects.toThrow(/429/);
    expect(state.calls).toBe(1);
  });

  test("returns the documented null on 404 without retrying", async () => {
    const state = stubFetch([() => new Response("", { status: 404 })]);

    const envelope = await apiGet("/x");
    expect(envelope).toBeNull();
    expect(state.calls).toBe(1);
  });

  test("gives up after the initial attempt plus two retries on persistent 5xx", async () => {
    instantTimers();
    const state = stubFetch([() => new Response("", { status: 500 })]);

    await expect(apiGet("/x")).rejects.toThrow(/500/);
    expect(state.calls).toBe(3);
  });

  test("fails fast on a connection error - no retry, per the graceful-degradation contract", async () => {
    const state = { calls: 0 };
    globalThis.fetch = (async () => {
      state.calls++;
      throw new TypeError("Unable to connect");
    }) as unknown as typeof fetch;

    await expect(apiGet("/x")).rejects.toThrow(/could not reach the freehire API/);
    expect(state.calls).toBe(1);
  });
});
