import {
  SEARCH_URL,
  htmlFetch,
  RateLimited,
  requestMeta,
  parseJobCards,
  jobageToTPR,
  minutesToTPR,
  workTypeFlag,
  writeError,
  type JobCard,
} from "../helpers.js"

export interface SearchOpts {
  query?: string
  location: string
  jobage: number
  jobageMinutes?: number
  remote?: string // "remote" | "hybrid" | "onsite"
  sort?: "date" | "relevance"
  page: number
  limit?: number
  format: "json" | "table" | "plain"
}

function buildUrl(opts: SearchOpts): string {
  const params = new URLSearchParams()
  if (opts.query) params.set("keywords", opts.query)
  if (opts.location) params.set("location", opts.location)
  const tpr = opts.jobageMinutes !== undefined ? minutesToTPR(opts.jobageMinutes) : jobageToTPR(opts.jobage)
  if (tpr) params.set("f_TPR", tpr)
  const wt = workTypeFlag(opts.remote)
  if (wt) params.set("f_WT", wt)
  params.set("sortBy", opts.sort === "date" ? "DD" : "R")
  params.set("start", String((opts.page - 1) * 10))
  return `${SEARCH_URL}?${params.toString()}`
}

function renderTable(cards: JobCard[]): string {
  if (cards.length === 0) return "No results."
  const rows = cards.map((c) => {
    const title = (c.title || "").slice(0, 42).padEnd(42)
    const company = (c.company || "—").slice(0, 26).padEnd(26)
    const loc = (c.location || "—").slice(0, 24).padEnd(24)
    const date = c.date || "—"
    return `${c.id.padEnd(11)} ${title} ${company} ${loc} ${date}`
  })
  const header =
    "ID".padEnd(11) +
    " " +
    "TITLE".padEnd(42) +
    " " +
    "COMPANY".padEnd(26) +
    " " +
    "LOCATION".padEnd(24) +
    " DATE"
  return [header, "-".repeat(header.length), ...rows].join("\n")
}

export async function runSearch(opts: SearchOpts): Promise<number> {
  requestMeta.http_attempts = 0
  requestMeta.retries = 0
  try {
    const html = await htmlFetch(buildUrl(opts))
    let cards = parseJobCards(html)
    if (html.trim() && cards.length === 0) {
      writeError("Search returned unexpected nonempty HTML without job cards", "SEARCH_PARSE_FAILED")
      return 1
    }
    if (opts.limit !== undefined && opts.limit >= 0) cards = cards.slice(0, opts.limit)

    if (opts.format === "table") {
      process.stdout.write(renderTable(cards) + "\n")
    } else if (opts.format === "plain") {
      process.stdout.write(
        cards
          .map(
            (c) =>
              `${c.title}\n  ${c.company || "—"} · ${c.location || "—"} · ${c.date || "—"}\n  id: ${c.id}\n  ${c.url}`,
          )
          .join("\n\n") + "\n",
      )
    } else {
      process.stdout.write(
        JSON.stringify(
          { meta: { count: cards.length, page: opts.page, ...requestMeta }, results: cards },
          null,
          2,
        ) + "\n",
      )
    }
    return 0
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), e instanceof RateLimited ? "RATE_LIMITED" : "SEARCH_FAILED")
    return 1
  }
}
