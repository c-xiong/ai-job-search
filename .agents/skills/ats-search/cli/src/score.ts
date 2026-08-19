// `prefit`: a deterministic 0-100 display prior, computed from signals the
// collector already has. It exists because a click produces a batch you want to
// read top-down *before* `/rank` (an LLM step) has run, and the board's `fit`
// column arrives empty from any collector.
//
// It is explicitly NOT a fit assessment. It never gates and never drops a
// posting, it always ships with the reasons that produced it, and `/rank`
// overwrites it as the authority (§12).

import type { FilteredPosting } from "./filters.ts"

/** Terms that make a title interesting on their own. */
const CORE_TERMS = [
  "machine learning", "ml", "nlp", "llm", "ai", "artificial intelligence",
  "research", "scientist", "data", "applied",
]
/** Terms that identify it as an engineering role. */
const ROLE_TERMS = ["engineer", "developer", "software", "backend", "full stack", "fullstack", "intern"]
/** Not excluded, but a graduate should not be sorted to the top of them. */
const SENIORITY_DEMOTIONS = /(?:^|[^a-z])(senior|sr|lead|manager|architect|expert)(?:[^a-z]|$)/i

/** Profile keywords worth a bump when the vendor ships the description inline. */
const PROFILE_KEYWORDS = [
  "llm", "rag", "retrieval-augmented", "nlp", "pytorch", "transformers", "hugging face",
  "sentence-transformers", "spacy", "embedding", "vector", "langchain", "agentic", "agent",
  "fastapi", "react", "next.js", "typescript", "docker", "aws", "evaluation", "forecasting",
]

export interface PrefitConfig {
  cities: string[]
}

export interface Prefit {
  score: number
  reasons: string[]
}

function termHit(text: string, terms: string[]): string | null {
  const lower = text.toLowerCase()
  for (const term of terms) {
    const re = new RegExp(`(?:^|[^a-z0-9])${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:[^a-z0-9]|$)`, "i")
    if (re.test(lower)) return term
  }
  return null
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value))
}

/**
 * @param item     a posting that already passed the filters
 * @param tier     the company's registry tier (1-5)
 * @param today    ISO reference date for recency (passed in: this stays pure)
 */
export function prefit(item: FilteredPosting, tier: number, today: string, cfg: PrefitConfig): Prefit {
  const reasons: string[] = []
  const title = item.posting.title

  // Title, 0-40.
  const core = termHit(title, CORE_TERMS)
  const role = termHit(title, ROLE_TERMS)
  let titleScore = 0
  if (role) titleScore += 18
  if (core) titleScore += 18
  if (role && core) titleScore += 4
  if (SENIORITY_DEMOTIONS.test(title)) {
    titleScore -= 10
    reasons.push("seniority wording in the title")
  }
  titleScore = clamp(titleScore, 0, 40)
  if (core && role) reasons.push(`title matches "${core}" + "${role}"`)
  else if (core || role) reasons.push(`title matches "${core ?? role}"`)

  // Tier, 0-25. Tier 1 full, tier 5 nothing.
  const tierScore = clamp(Math.round((25 * (5 - tier)) / 4), 0, 25)
  if (tier <= 2) reasons.push(`tier ${tier} company`)

  // Location, 0-15.
  let locationScore = 3
  const matched = item.matched
  const targetCities = cfg.cities.map((c) => c.toLowerCase())
  if (matched?.city && targetCities.includes(matched.city.toLowerCase())) {
    locationScore = 15
    reasons.push(`target city ${matched.city}`)
  } else if (matched?.country && !item.location_uncertain) {
    locationScore = 11
    reasons.push(`allowed country ${matched.country}`)
  } else if (matched?.region && !item.location_uncertain) {
    locationScore = 7
    reasons.push(`remote ${matched.region}`)
  } else {
    reasons.push("location unresolved")
  }

  // Recency, 0-10. A missing date scores neutral, never zero: an undated posting
  // is an unknown, and scoring it zero would bury every Personio board.
  let recencyScore = 5
  if (item.posting.date) {
    const age = Math.floor((Date.parse(today) - Date.parse(item.posting.date)) / 86400000)
    recencyScore = age <= 7 ? 10 : age <= 14 ? 8 : age <= 30 ? 6 : age <= 60 ? 3 : 1
    if (age <= 7) reasons.push("posted this week")
  }

  // Description keywords, 0-10, only when the vendor shipped one.
  let keywordScore = 0
  const description = item.posting.description
  if (description) {
    const lower = description.toLowerCase()
    const hits = PROFILE_KEYWORDS.filter((k) => lower.includes(k))
    keywordScore = clamp(hits.length * 2, 0, 10)
    if (hits.length >= 3) reasons.push(`profile keywords: ${hits.slice(0, 3).join(", ")}`)
  }

  const score = clamp(titleScore + tierScore + locationScore + recencyScore + keywordScore, 0, 100)
  // A number never appears without its explanation, but three reasons is plenty.
  return { score, reasons: reasons.slice(0, 3) }
}
