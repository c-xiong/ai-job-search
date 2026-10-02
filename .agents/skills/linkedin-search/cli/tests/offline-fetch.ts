// CLI validation tests must never issue live requests.
globalThis.fetch = (async () => new Response("")) as typeof fetch;
