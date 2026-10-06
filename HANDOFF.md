# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## State
Phase 7 (clean UI and hosting) is complete. The app is live at
https://kb-finder.nebanebish.com (Vercel Hobby, iad1; Supabase free
tier, us-east-1).
The old https://kb-finder-drab.vercel.app still works.
- It sits behind a 4-digit PIN gate with a global cap of 10 failed
  logins per UTC day (D2 revised again, DECISIONS.md 2026-10-04,
  committed in 99eab6a).
- Semantic is the hosted default through `DEFAULT_SEARCH_MODE`. The UI
  no longer hard-codes a mode; it sends the server's default.
- Step 15 (deploy and hosted checks) is done; step 16, the UI pass
  (D7), remains.
- DECISIONS.md has the Phase 7 entries under "2026-10-04: Phase 7
  hosting decisions" (D1 to D12 and the related rules). Code comments
  cite D10.
- DECISIONS.md is the source of truth for every number. Do not copy
  numbers from anywhere else.

## Phase 7 commits since f90ce1c
- 8be52c0 db: lock down Data API grants and enable RLS
- 12ba787 db: add least-privilege kb_app role and rate-limit table
- 6c829f6 db: enable local transaction pooler for the hosted path
- 6fd1882 api: cap search request bodies at 32 KiB
- 86f4eb8 api: add global daily search cap
- 7aa2570 app: add password access gate
- 6c21d86 db: verify TLS against the Supabase CA when hosted
- 872196b scripts: add read-only database fingerprint
- 33cb624 ui: use the server default mode and show fixed error messages
- d97e085 db: pin search_path on kb_ functions
- 095b25d search: check the stored embedding config before embedding
- 7a2b261 agents: deny bare python commands
- bb24232 docs: document Phase 7 hosting and security
- 1069c6c docs: log Phase 7 hosting decisions
- 7ce5908 scripts: add sampled-logging settings to the fingerprint
- 99eab6a gate: allow a 4-digit PIN with a global failed-login cap
- 15d21a7 docs: record the PIN gate and failed-login cap
- e2d37a3 docs: record the verified hosted deploy

## What was built
- **Migrations** (the two pinned retrieval migrations are untouched;
  all six are applied on hosted):
  - `20261001182428_lock_down_data_api.sql`: `anon`, `authenticated`
    and PUBLIC lose every privilege in the schema; postgres's default
    privileges stop granting them; a global default-privilege revoke
    of EXECUTE from PUBLIC for future postgres routines; RLS on every
    table. `service_role` is left as Supabase sets it.
  - `20261001190406_add_app_role.sql`: `kb_app` (NOLOGIN, no
    password, `statement_timeout` 5000 ms and `search_path` on the
    role), SELECT on the KB tables, EXECUTE on the two search functions
    and two helpers, `kb_rate_counters`, and RLS policies only for
    `kb_app` (D10).
  - `20261003075008_pin_function_search_path.sql`: `ALTER FUNCTION ...
    SET search_path FROM CURRENT` on the five `kb_` functions, using a
    transaction-local set and restore. Function bodies are unchanged.
  - `20261004232626_add_gate_fail_kind.sql`: drops and re-adds the two
    `kb_rate_counters` CHECKs by name to allow kind
    `gate_fail_global_day` (key `all`). Idempotent, no grant changes.
- **Local pooler:** `supabase/config.toml` enables `[db.pooler]` on
  54329, and the start command no longer excludes Supavisor.
- **Request handling** (`lib/search/handler.ts`), in order: config
  checks (500), gate (401), 32 KiB body cap (413), schema and mode
  (400), daily cap of 200 valid searches per UTC day (429), KBF02
  config check (semantic), retrieval. Every body is fixed.
- **Gate:** `lib/gate/cookie.ts`, `lib/gate/login.ts`,
  `app/api/gate/route.ts`, `app/gate/*`, `proxy.ts`. Shared password
  of exactly 4 ASCII digits or 20+ characters, `kbf_gate` cookie with
  sha256(password) in the MAC, SameSite=Lax, 7 days.
- **Failed-login cap:** `lib/limits/gate-fail-cap.ts`, used by
  `lib/gate/login.ts`. 10 per UTC day (`GATE_FAIL_CAP`). Reserve
  (the daily cap's upsert) before the compare, release (one MERGE) on
  success; attempt 11 is 429 `{"error":"Too many attempts"}` and is
  never compared. A reserve error is 500; a release error is logged
  and the login still succeeds. The gate form shows a fixed message on
  429.
- **Daily cap:** `lib/limits/*`. One upsert statement, pooler safe,
  global counters only, no IP stored.
- **TLS:** `lib/db.ts` verifies against `DATABASE_CA_PEM` when
  `VERCEL` is set and refuses a `DATABASE_URL` with a query string.
- **KBF02 server check:** `lib/search/semantic.ts` compares the stored
  `embedding_configs` row with the config file before embedding.
- **UI:** `app/page.tsx` resolves the default mode on the server;
  `app/search-client.tsx` maps 401, 413, 429 and other failures to
  fixed messages.
- **`scripts/db_fingerprint.py`:** read-only fingerprint
  (`db-fingerprint-v2`) for diffing local against hosted.
- **Agent settings:** `.claude/settings.json` denies bare `python`,
  `python3` and `py`.

## Tests (last run on the PIN-gate code, 99eab6a)
- Python: the full suite, 387 tests OK.
- `npm run test:unit`: 361 passed.
- `npm run test:db`: 21 passed, with `KB_FINDER_TEST_DATABASE_URL` set
  to local.
- No code changed after 99eab6a; later commits are docs only.
- No test calls OpenAI. Web tests block real fetch. The embedding
  tests block the network and never build a real client.
- Tests that read edited files: `tests/web/db.config.unit.test.ts`,
  `tests/web/gate.unit.test.ts` and
  `tests/web/search.semantic.unit.test.ts` read `.env.example`
  (names present, no assignments, no `NEXT_PUBLIC_`). All pass.

## Known limitations
- **Small set:** 21 positives, 4 negatives. One case moves a metric by
  about 5 points. Track A met the rule exactly (16/21 against 16).
- **No abstention:** semantic always returns up to 50 results.
- **Reruns can differ:** query embeddings are not cached.
- **Local-only cache:** the article embedding cache lives only on the
  project owner's machine, pinned by sha256 in DECISIONS.md.
- **KBF05 cost:** the per-request staleness check costs about 47 ms.
- **One shared password or PIN, global failed-login cap:** fine for
  the owner plus 2 or 3 testers. Anyone can lock out new logins until
  the next UTC day by failing 10 times; existing cookies keep working.
  Rotate the password or the cookie secret to revoke every cookie;
  rotate the PIN after AfroTech (D2).
- **Global cap only:** one tester can use the whole day's 200
  searches. A search that fails after the cap check still counts.
- **Free-tier pause:** hosted Supabase pauses after 7 idle days; check
  it by hand before demos (D9).
- **Setup quirks:** `supabase start -x` still pulls some excluded
  images. There is no `seed.sql`. `next dev` needs
  `NEXT_TELEMETRY_DISABLED` set in the shell. Vite prints a
  configLoader warning.

## What remains
- **Step 14, DONE:** the Codex checkpoint 4 review. No critical, high,
  medium or low findings; one informational finding (sampled-logging
  settings), fixed in 7ce5908 "scripts: add sampled-logging settings
  to the fingerprint".
- **Step 15, DONE:** deploy and hosted checks. Detail is in
  DECISIONS.md "2026-10-04: Phase 7 step 15, hosted deploy verified".
  In short: the hosted fingerprint is identical to the local v2
  reference; `kb_app` passes 19/19 checks with the 5s
  `statement_timeout`; hosted logging settings pass, so D8 A2 is not
  needed; TLS verify-full works for the app; the sentinel search is
  absent from the logs; `kb_rate_counters` holds no IP; the Data API
  is disabled.
- **Step 16, next:** the UI pass (D7). Visual only, mobile first,
  time-boxed to 1 to 2 days, no eval queries as examples.
- **Follow-ups:** rotate the PIN after AfroTech; check the Supabase
  project is not paused before demos (D9).
- **Phase 6, query normalization:** a stretch goal, only after Phase 7.
- **Separate, logged experiments:** hybrid retrieval, reranking, and a
  new, separately versioned eval set.

## Deploy rules
- Hosted env vars are Production only and set in Vercel.
- `RATE_LIMIT`, `ACCESS_GATE` and `EVAL_MODES_ENABLED` must never be
  set there.
- Keep "System Environment Variables" exposed: `VERCEL` drives the
  fail-closed rules.
- `DATABASE_URL` has no query string.
- Any env change needs a redeploy.

## Unresolved human decisions
- Whether staffOnly text is searchable (currently excluded in both
  modes).
- When to version a new eval set (case_013 and case_025 labels).
