# `ats-search` — endpoints, payload shapes, and access notes

Everything here was verified live on **2026-08-18** against the endpoints below.
When a vendor changes its schema, this is the file to update first, then the
fixtures (`cli/tests/fixtures/`, refresh with `capture.sh`).

---

## Access check per vendor host

Run with `python3 tools/robots_check.py <endpoint-url>`, which implements RFC 9309
on the cautious side (longest match wins, `*` or `Claude-User` Disallow blocks,
404 = no published policy = permission, any other read failure = unconfirmed).

| Host | Verdict (2026-08-18) | Terms | Consequence |
|---|---|---|---|
| `boards-api.greenhouse.io` | **ALLOWED** — robots.txt permits the board path | <https://www.greenhouse.io/legal/terms-of-service> | enabled |
| `api.ashbyhq.com` | **UNCONFIRMED** — `robots.txt` answers HTTP 401, so no policy can be read | <https://www.ashbyhq.com/terms> | enabled, low volume. RFC 9309 treats an unreadable 4xx as "no applicable policy" (access permitted); this repo's checker is stricter and reports UNCONFIRMED. Both readings are recorded rather than picking the flattering one. Revisit if Ashby publishes a policy. |
| `*.jobs.personio.de` | **ALLOWED** — no robots.txt published on the board subdomains | <https://www.personio.com/terms-and-conditions/> | enabled |
| `api.lever.co` | **ALLOWED** — robots.txt permits the postings path | <https://www.lever.co/legal/terms-of-service/> | enabled |
| `api.smartrecruiters.com` | **DISALLOWED** | <https://www.smartrecruiters.com/legal/terms-of-service/> | **DISABLED by default** |

### The SmartRecruiters finding, in full

```
User-agent: LinkedInBot
Allow: /v1/companies/

User-agent: *
Disallow: /
```

Every agent except LinkedIn's crawler is disallowed from the whole host,
including `/v1/companies/{token}/postings`. The adapter is implemented and tested, but it is **fail-closed**: the policy is
compiled into the CLI (`cli/src/registry.ts`, `POLICY_BLOCKED`), so a fresh
`companies.json` copied from the example - or one whose `vendor_access` block was
deleted - still does not reach the host. Naming the company on the command line
does not get round it either, and neither does `detail`, which reaches the same
endpoint. A published policy is not a preference.

⚠️ **Turning it on is a personal-use decision and your own responsibility.** If
you do, keep the volume tiny (one board, rarely), never bulk or commercial. The
only company affected today is Nexthink, whose identity is verified either way.

The honest comparison that survives: LinkedIn's terms prohibit automated access
outright, which is why `linkedin-search` carries its warning. Three of these five
endpoints exist to be read by machines and say so; one does not; one will not say.

---

## Greenhouse

```
GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}          # one posting
```

```jsonc
{ "jobs": [ {
    "id": 4635890101,                 // number, stringified for the composite id
    "internal_job_id": 4123456,
    "title": "Business Development Representative",
    "company_name": "Parloa",         // <- the independent identifier (§8 rank 2)
    "absolute_url": "https://job-boards.eu.greenhouse.io/parloa/jobs/4635890101",
    "requisition_id": "85",
    "updated_at": "2026-07-26T09:26:20-04:00",
    "first_published": "2025-07-10T17:48:58-04:00",   // <- THE POSTING DATE
    "application_deadline": null,
    "location": { "name": "New York Office" },
    "metadata": [ { "id": 1, "name": "...", "value": "...", "value_type": "..." } ],
    "language": "en",
    "content": "&lt;p&gt;…&lt;/p&gt;"  // present with ?content=true; HTML-escaped HTML
  } ],
  "meta": { "total": 48 } }
```

- **`first_published` is the posting date.** `updated_at` moves on every content
  edit; substituting it would make an old posting look new on every run. It is
  kept in its own field and never used as `date`.
- **EU tenants** answer on `job-boards.eu.greenhouse.io` — `canonical_url()` folds
  that and `boards.greenhouse.io` onto one form.
- `content` is **double-escaped**: entity-decode, then strip tags.
- `application_deadline` exists here and is worth keeping (the tracker has a
  deadline column).

## Ashby

```
GET https://api.ashbyhq.com/posting-api/job-board/{token}
```

```jsonc
{ "apiVersion": "1",
  "jobs": [ {
    "id": "2d78e7c6-3764-482e-9d29-d0cb064429cc",
    "title": "Executive Assistant (f/m/d) to Co-CRO",
    "department": "Research", "team": "Research", "employmentType": "FullTime",
    "location": "Heidelberg",
    "secondaryLocations": [],          // [{ "location": "...", "address": {...} }]
    "publishedAt": "2026-06-19T16:52:14.872+00:00",
    "isListed": true,
    "isRemote": true,                  // unreliable on its own: true here while workplaceType is Hybrid
    "workplaceType": "Hybrid",
    "address": { "postalAddress": {
        "addressRegion": "Baden Würtemberg",
        "addressCountry": "Germany",   // a country NAME, not ISO-2
        "addressLocality": "Heidelberg" } },
    "jobUrl": "https://jobs.ashbyhq.com/alephalpha/2d78…",
    "applyUrl": "https://jobs.ashbyhq.com/alephalpha/2d78…/application",
    "descriptionHtml": "…", "descriptionPlain": "…"
  } ] }
```

- **No company identifier anywhere.** An Ashby board can only reach `verified`
  through a link on the company's own site or your explicit confirmation.
- `workplaceType` beats `isRemote`; the Aleph Alpha board ships `isRemote: true`
  on a Hybrid posting.
- `addressCountry` is useful for the country filter and is **not** identity
  evidence — "a plausible-looking country" is explicitly not evidence.
- `isListed: false` postings are dropped.
- Payloads get large (255 postings for ElevenLabs, ~300 KB).

## Personio

```
GET https://{token}.jobs.personio.de/xml
```

```xml
<workzag-jobs>
  <position>
    <id>1226329</id>
    <subcompany>unique land use GmbH</subcompany>
    <office>Freiburg</office>
    <department>All departments</department>
    <recruitingCategory>Angestellte / Employee</recruitingCategory>
    <name>Employee Position - initiative application …</name>
    <jobDescriptions>
      <jobDescription><name>…</name><value><![CDATA[<ul><li>…</li></ul>]]></value></jobDescription>
    </jobDescriptions>
    <employmentType>permanent</employmentType>
    <seniority>experienced</seniority>
    <schedule>full-or-part-time</schedule>
    <yearsOfExperience>2-5</yearsOfExperience>
    <occupation>program_management</occupation>
    <occupationCategory>project_and_program_management</occupationCategory>
    <createdAt>2023-08-21T15:49:07+00:00</createdAt>
  </position>
</workzag-jobs>
```

- **A non-customer subdomain answers HTTP 307**, not 404 → classified `not_found`.
  The transport uses `redirect: manual` precisely so this is visible.
- `jobDescriptions` can be **empty**; descriptions are CDATA-wrapped HTML.
- The block contains its own `<name>` elements, so it is stripped out before the
  position's scalar fields are read — otherwise a section heading becomes the job
  title.
- **The identity trap, live:** `unique.jobs.personio.de` resolves, and belongs to
  *unique land use GmbH* (Freiburg), **not** to Unique AG (Zurich). The subdomain
  proves a subdomain exists, not whose it is. This is the single best argument for
  the §8 evidence hierarchy and it is recorded in the registry as a rejected
  candidate.

## Lever

```
GET https://api.lever.co/v0/postings/{token}?mode=json
GET https://api.lever.co/v0/postings/{token}/{id}?mode=json
```

```jsonc
[ { "id": "54743786-1ad7-4be7-bcbe-67f660e9c381",
    "text": "Account Based Marketing (ABM) Manager",
    "createdAt": 1785155309260,        // EPOCH MILLISECONDS, not a string
    "country": "GB",                   // ISO-2, the cleanest country field of the five
    "workplaceType": "onsite",
    "categories": { "commitment": "Employee / Full-Time", "department": "GoToMarket",
                    "location": "London", "team": "Digital & Web",
                    "allLocations": ["London"] },
    "descriptionPlain": "…", "description": "<p>…</p>",
    "lists": [ { "text": "Responsibilities", "content": "<li>…</li>" } ],
    "additionalPlain": "…", "additional": "…",
    "descriptionBodyPlain": "…", "opening": "", "openingPlain": "",
    "hostedUrl": "https://jobs.lever.co/sonarsource/5474…",
    "applyUrl":  "https://jobs.lever.co/sonarsource/5474…/apply" } ]
```

- Top level is a **bare array**, not an envelope.
- The full text is `descriptionPlain` + each `lists[].text`/`content` +
  `additionalPlain`; reading only `descriptionPlain` loses the requirements.
- An unknown token 404s cleanly — the friendliest failure of the five.

## SmartRecruiters *(adapter implemented, disabled by policy — see above)*

```
GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N
GET https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}     # jobAd sections
```

```jsonc
{ "offset": 0, "limit": 100, "totalFound": 97,
  "content": [ {
    "id": "744000144073609", "uuid": "40d96871-…", "refNumber": "REF3392T",
    "name": "Director, Global Procurement",
    "releasedDate": "2026-08-18T13:27:19.818Z",
    "company": { "identifier": "Nexthink", "name": "Nexthink" },   // <- §8 rank 2
    "location": { "city": "Amsterdam", "region": "NH", "country": "nl",
                  "remote": true, "hybrid": false },
    "typeOfEmployment": { "id": "permanent", "label": "Full-time" },
    "experienceLevel": { "id": "director", "label": "Director" },
    "industry": {…}, "department": {…}, "function": {…}, "language": {…}
  } ] }
```

- **The false 200, verified against a nonsense slug:**
  `{"offset":0,"limit":2,"totalFound":0,"content":[]}` with HTTP 200. A wrong
  guess is indistinguishable from an empty board, so `totalFound === 0` is
  reported `not_found`, never `empty` — the conservative direction, because
  `empty` would keep a mis-resolved company quietly in rotation forever.
- `location.country` is **lowercase** ISO-2.
- The only v1 vendor that paginates, and the only one with **no description** in
  the listing — descriptions need the per-posting `jobAd.sections` call.

---

## Phase 2 backlog (no adapter in v1)

| Vendor | Endpoint | Why it is deferred |
|---|---|---|
| recruitee | `GET {token}.recruitee.com/api/offers/` | One known company (Personio, 1 posting). Trivial later. |
| workday | `POST {tenant}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | POST bodies, tenant/site discovery, offset pagination, and `postedOn` as **relative text** ("Posted 5 Days Ago") which must become `null`, never a synthesized timestamp. Unlocks Roche, Novartis, Siemens, Swiss Re, Zurich Insurance, UBS, Julius Baer. |
| successfactors / custom | — | SAP, Zalando, Amazon, the Dublin big-tech cluster. |
