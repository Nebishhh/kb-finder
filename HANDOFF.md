# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## State
Phase 7 (clean UI and hosting) is complete and deployed, including
step 16, the UI pass (D7). The app is live at
https://kb-finder.nebanebish.com (Vercel Hobby, iad1; Supabase free
tier, us-east-1).
The old https://kb-finder-drab.vercel.app still works.
- The custom domain is a Vercel custom domain on the Production
  deployment: one CNAME record, kb-finder, in the cPanel Zone Editor of
  the Namecheap web hosting. No code or env changes. The gate cookie
  belongs to the exact host, so everyone signs in once on the new
  domain (DECISIONS.md "2026-10-06: Custom domain").
- It sits behind a 4-digit PIN gate with a global cap of 10 failed
  logins per UTC day (D2 revised again, DECISIONS.md 2026-10-04).
- Semantic is the hosted default through `DEFAULT_SEARCH_MODE`. The UI
  sends the server's default and shows it as a header label
  ("Semantic search").
- Retrieval work stays frozen until after AfroTech (DECISIONS.md
  2026-10-05, D7 revised again).
- DECISIONS.md is the source of truth for every number. Do not copy
  numbers from anywhere else.

## Commits since e2d37a3
- 324a328 docs: update HANDOFF after the hosted deploy
- 0a219e3 docs: log D7 revision for the UI design phase
- 1b06a70 ui: restyle the search page (D7)
- 8e6ce76 ui: restyle the gate and finalize copy (D7)
- eec203e ui: add Enter to search, favicon and lede fix (D7)
- bb8189c docs: record the step 16 UI pass
- 5e7df4b ui: add Neba link to the footer and cap the gate field (D7)
- f69b7ea docs: record the custom domain

## What was built
- **Step 16, UI pass (D7):** detail is in DECISIONS.md "2026-10-05:
  Phase 7 step 16, UI pass done".
  - Design phase first; one direction ("Routing index") chosen and
    frozen: cool near-white canvas, graphite text, hairline rules,
    square shapes, no shadows. Blue only for links and the routing
    line; amber only for warnings.
  - Atkinson Hyperlegible Next via next/font (downloaded at build
    time; browsers never call Google). Dark mode follows the system
    setting; no toggle. Plain CSS with custom properties; no new
    dependencies.
  - The score, "Mode:" and "Showing X of 50" are gone. Results show
    "Top N matches" with a line saying results always appear, because
    there is no abstention. Enter searches (Shift+Enter is a newline),
    with no `<form>` around the textarea, so the query never reaches a
    URL.
  - The proxy matcher also excludes icon.svg (whole-segment rule, like
    favicon.ico), covered by matcher tests on the real config.
  - 5e7df4b: the footer has a "Neba" link (to the owner's GitHub)
    next to the disclaimer, and the access-code field is capped at
    360px wide from 640px up. `tests/web/layout.unit.test.ts` covers
    the footer.
  - Unchanged: the API, ranking, gate logic, caps and privacy rules.
    No example queries were added (NOTES.md).
- **Migrations** (the two pinned retrieval migrations are untouched;
  all six are applied on hosted):
  - `20261001182428_lock_down_data_api.sql`: `anon`, `authenticated`
    and PUBLIC lose every privilege in the schema; a global
    default-privilege revoke of EXECUTE from PUBLIC for future postgres
    routines; RLS on every table. `service_role` is left as Supabase
    sets it.
  - `20261001190406_add_app_role.sql`: `kb_app` (`statement_timeout`
    5000 ms on the role), SELECT on the KB tables, EXECUTE on the
    search functions, write access only to `kb_rate_counters`, and RLS
    policies only for `kb_app` (D10).
  - `20261003075008_pin_function_search_path.sql`: pins `search_path`
    on the five `kb_` functions (D6). Bodies unchanged.
  - `20261004232626_add_gate_fail_kind.sql`: replaces the two
    `kb_rate_counters` CHECKs to allow kind `gate_fail_global_day`.
- **Local pooler:** `supabase/config.toml` enables `[db.pooler]` on
  54329 (D12).
- **Request handling** (`lib/search/handler.ts`): config checks, gate
  (401), 32 KiB body cap (413), schema and mode (400), daily cap of 200
  valid searches per UTC day (429), KBF02 config check (semantic),
  retrieval. Every error body is fixed.
- **Gate:** `lib/gate/*`, `app/api/gate/route.ts`, `app/gate/*`,
  `proxy.ts`. Exactly 4 ASCII digits or 20+ characters; `kbf_gate`
  cookie with sha256(password) in the MAC, SameSite=Lax, 7 days.
- **Failed-login cap:** `lib/limits/gate-fail-cap.ts`. 10 per UTC day;
  reserve before the compare, release on success; attempt 11 is 429 and
  is never compared.
- **TLS:** `lib/db.ts` verifies against `DATABASE_CA_PEM` when
  `VERCEL` is set and refuses a `DATABASE_URL` with a query string.
- **`scripts/db_fingerprint.py`:** read-only fingerprint
  (`db-fingerprint-v2`) for diffing local against hosted.

## Tests
- Not run in this session (docs-only changes), so the current counts
  are unknown.
- Last recorded (DECISIONS.md, step 16): unit tests 400 passing;
  `npm run test:db` not run for step 16 (no DB or SQL changes). That
  count predates 5e7df4b, which added `tests/web/layout.unit.test.ts`.
- No test calls OpenAI. Web tests block real fetch. The embedding
  tests block the network and never build a real client.

## Known limitations
- **Small set:** 21 positives, 4 negatives. One case moves a metric by
  about 5 points. Track A met the semantic rule exactly (Top-3 16/21
  against 16).
- **No abstention:** semantic always returns results; the UI says so.
- **Reruns can differ:** query embeddings are not cached.
- **Local-only cache:** the article embedding cache lives only on the
  project owner's machine, pinned by sha256 in DECISIONS.md.
- **KBF05 cost:** the per-request staleness check costs about 47 ms.
- **One shared PIN, global failed-login cap:** anyone can lock out new
  logins until the next UTC day by failing 10 times; devices already
  logged in keep working. Rotate the password or the cookie secret to
  revoke every cookie.
- **Global cap only:** one tester can use the whole day's 200
  searches. A search that fails after the cap check still counts.
- **Free-tier pause:** hosted Supabase pauses after 7 idle days (D9).
- **UI quirks (NOTES.md):** Samsung Internet and some in-app browsers
  force their own dark mode; demo in regular Chrome. Browsers cache
  favicons hard; check in an incognito tab. The UI copy hardcodes
  "3,109" in `app/search-client.tsx`, `app/page.tsx` and
  `app/gate/page.tsx`; update all three if the KB is re-collected.
- **Setup quirks:** `supabase start -x` still pulls some excluded
  images. There is no `seed.sql`. `next dev` needs
  `NEXT_TELEMETRY_DISABLED` set in the shell. Vite prints a
  configLoader warning.

## What remains
- **Steps 14, 15 and 16: DONE.** Step 15 and 16 detail is in
  DECISIONS.md.
- **Follow-ups:**
  - Rotate the PIN after AfroTech (D2).
  - Check the Supabase project is not paused before demos (D9).
- **Parked, after AfroTech:**
  - Eval v2: a new, separately versioned eval set, with a comparison
    against the native KB search. Exclude any example queries logged
    in NOTES.md.
  - Then hybrid retrieval or reranking, each as a separate, logged
    experiment.
- **Phase 6, query normalization:** still planned later (AGENTS.md).

## Deploy rules
- Hosted env vars are Production only and set in Vercel.
- `RATE_LIMIT`, `ACCESS_GATE` and `EVAL_MODES_ENABLED` must never be
  set there.
- Keep "System Environment Variables" exposed: `VERCEL` drives the
  fail-closed rules.
- `DATABASE_URL` has no query string.
- Any env change needs a redeploy.
- No AI assistant open on screens that show secrets; secrets go only
  into the password manager and Vercel (DECISIONS.md step 15).

## Unresolved human decisions
- Whether staffOnly text is searchable (currently excluded in both
  modes).
- When to version a new eval set (case_013 and case_025 labels).
