// `detail` - one posting, by composite id or by posting URL (§10.2).
//
// A bare vendor posting id is rejected. Across ~55 companies and five vendors it
// is not unique, and guessing which board it belongs to is how a detail lookup
// quietly returns a different company's job.

import { parseCompositeId, transport, writeError } from "../helpers.ts"
import { loadRegistry, vendorAccess } from "../registry.ts"
import type { Transport } from "../helpers.ts"
import { adapterFor } from "../vendors/index.ts"
import type { VendorName } from "../types.ts"

interface Target {
  vendor: VendorName
  token: string
  postingId: string
}

/** Recover (vendor, token, posting) from a posting URL - the other accepted form. */
export function parsePostingUrl(input: string): Target | null {
  const url = input.trim()
  const patterns: Array<[VendorName, RegExp]> = [
    ["greenhouse", /(?:job-boards(?:\.eu)?|boards)\.greenhouse\.io\/([^/?#]+)\/jobs\/([^/?#]+)/i],
    ["ashby", /jobs\.ashbyhq\.com\/([^/?#]+)\/([^/?#]+)/i],
    ["personio", /([A-Za-z0-9-]+)\.jobs\.personio\.(?:de|com)\/job\/([^/?#]+)/i],
    ["lever", /jobs\.(?:eu\.)?lever\.co\/([^/?#]+)\/([^/?#]+)/i],
    ["smartrecruiters", /jobs\.smartrecruiters\.com\/([^/?#]+)\/([^/?#]+)/i],
  ]
  for (const [vendor, re] of patterns) {
    const m = url.match(re)
    if (m) return { vendor, token: m[1], postingId: m[2] }
  }
  return null
}

export interface DetailOpts {
  id: string
  format: "json" | "plain"
}

export async function runDetail(opts: DetailOpts, get: Transport = transport()): Promise<number> {
  const target = parseCompositeId(opts.id) ?? parsePostingUrl(opts.id)
  if (!target) {
    writeError(
      `"${opts.id}" is not a composite id or a posting URL. ats-search ids are ` +
        `<vendor>:<token>:<posting_id> (e.g. ashby:deepjudge:41c79388-fad4-4286-948c-b279c35c568d); ` +
        `a bare posting id is ambiguous across boards and is never guessed at.`,
      "AMBIGUOUS_ID",
    )
    return 1
  }
  const adapter = adapterFor(target.vendor)
  if (!adapter) {
    writeError(`no adapter for vendor ${target.vendor}`, "BAD_ARGS")
    return 1
  }
  // The access gate is per *host*, not per command. `detail` reaches the same
  // endpoint `search` does, so a vendor whose published policy said no is
  // refused here as well - and it fails closed when there is no registry at all,
  // because the policy lives in code (registry.ts POLICY_BLOCKED).
  let defaults
  try {
    defaults = loadRegistry().defaults
  } catch {
    defaults = { allowed_countries: [], remote_regions: [], cities: [], title_include: [],
                 title_exclude: [], conditional_exclude: [], cadence_days: {}, min_interval_minutes: 60 }
  }
  const access = vendorAccess(defaults, target.vendor)
  if (!access.enabled) {
    writeError(
      `${target.vendor} is disabled${access.why ? ` - ${access.why}` : ""}`,
      "VENDOR_DISABLED",
    )
    return 1
  }
  const posting = await adapter.fetchDetail(target.token, target.postingId, get)
  if (!posting) {
    writeError(`posting ${opts.id} not found on the ${target.vendor} board "${target.token}"`, "NOT_FOUND")
    return 1
  }
  const payload = {
    id: `${target.vendor}:${target.token}:${posting.posting_id}`,
    vendor: target.vendor,
    token: target.token,
    ...posting,
  }
  if (opts.format === "json") {
    process.stdout.write(JSON.stringify(payload, null, 2) + "\n")
  } else {
    process.stdout.write(
      [
        posting.title,
        `${posting.company ?? ""} - ${posting.locations.map((l) => l.text).join(" / ") || "(no location)"}`,
        `posted: ${posting.date ?? "unknown"}${posting.deadline ? `   deadline: ${posting.deadline}` : ""}`,
        posting.url,
        "",
        posting.description ?? "(no description on this board)",
        "",
      ].join("\n"),
    )
  }
  return 0
}
