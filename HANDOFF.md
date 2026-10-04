# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## State
Phase 7 (clean UI and hosting): the code is complete, the deploy is
pending.
- The code steps are committed (f90ce1c..7a2b261, below). Step 13 is
  this documentation pass (uncommitted).
- Steps 14 to 16 remain: the Codex checkpoint 4 review, the deploy
  and hosted checks, then the UI pass (D7).
- Semantic is the hosted default through `DEFAULT_SEARCH_MODE`. The UI
  no longer hard-codes a mode; it sends the server's default.
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
- (this session, uncommitted) docs: README, AGENTS, SPEC,
  .env.example and HANDOFF updated for Phase 7

## What was built
- **Migrations** (the two pinned retrieval migrations are untouched):
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
- **Local pooler:** `supabase/config.toml` enables `[db.pooler]` on
  54329, and the start command no longer excludes Supavisor.
- **Request handling** (`lib/search/handler.ts`), in order: config
  checks (500), gate (401), 32 KiB body cap (413), schema and mode
  (400), daily cap of 200 valid searches per UTC day (429), KBF02
  config check (semantic), retrieval. Every body is fixed.
- **Gate:** `lib/gate/cookie.ts`, `lib/gate/login.ts`,
  `app/api/gate/route.ts`, `app/gate/*`, `proxy.ts`. Shared password
  of 20+ characters, `kbf_gate` cookie with sha256(password) in the
  MAC, SameSite=Lax, 7 days, no login rate limit.
- **Daily cap:** `lib/limits/*`. One upsert statement, pooler safe,
  global counter only, no IP stored.
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

## Tests (this session, at 7a2b261 plus doc edits)
- `npm run lint`: clean.
- `npm run test:unit`: 316 passed, 7 files.
- Python: the full suite last ran at d97e085 (step 2), 386 tests OK,
  run by the project owner with `KB_FINDER_TEST_DATABASE_URL` set. No
  Python file changed after that commit.
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
- **One shared password, no login rate limit:** fine for the owner
  plus 2 or 3 testers; rotate the password or the cookie secret to
  revoke every cookie.
- **Global cap only:** one tester can use the whole day's 200
  searches. A search that fails after the cap check still counts.
- **Free-tier pause:** hosted Supabase pauses after 7 idle days; check
  it by hand before demos (D9).
- **Setup quirks:** `supabase start -x` still pulls some excluded
  images. There is no `seed.sql`. `next dev` needs
  `NEXT_TELEMETRY_DISABLED` set in the shell. Vite prints a
  configLoader warning.

## What remains
- **Step 14:** the Codex checkpoint 4 review (read-only, human-run).
- **Step 15, deploy and hosted checks (human-led):** hosted Supabase
  migrations, ingest and `embed_kb.py load --allow-remote`; set
  `kb_app` LOGIN and password by hand; Vercel env vars
  (`DATABASE_URL` with no query string, `DATABASE_CA_PEM`,
  `DEFAULT_SEARCH_MODE=semantic`, `ACCESS_PASSWORD`,
  `GATE_COOKIE_SECRET`, `OPENAI_API_KEY` Production only and
  Sensitive); compare `db_fingerprint.py` output against the local v2
  reference in DECISIONS.md.
- **Step 16:** the UI pass (D7).
- **Phase 6, query normalization:** a stretch goal, only after Phase 7.
- **Separate, logged experiments:** hybrid retrieval, reranking, and a
  new, separately versioned eval set.

## Unresolved human decisions
- Whether staffOnly text is searchable (currently excluded in both
  modes).
- When to version a new eval set (case_013 and case_025 labels).
