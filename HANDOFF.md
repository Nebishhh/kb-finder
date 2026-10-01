# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## State
Phase 4 slices 1 to 3 are done and committed. The first lexical
baseline has been run and recorded. main matches origin/main as of
the last fetch.

## Commits
- 0ff4941 docs: freeze phase 4 lexical baseline configuration
- 08fe595 feat: articles schema, lexical search SQL and ingestion (slice 1)
- b133def feat: lexical /api/search and bare search UI (slice 2)
- 3cabe8d docs: log slice 2 configuration and notes
- 40b23d7 feat: eval runner with metric and privacy tests (slice 3)
- c798c25 docs: record first lexical baseline result

## What was built
- Slice 1: `articles` table, stored `search_vector`, `kb_search_lexical`
  and `scripts/ingest_kb.py`. Migration
  `supabase/migrations/20260930042716_create_articles.sql`.
  3,109 articles ingested.
- Slice 2: Next.js app. `POST /api/search` calls
  `kb_search_lexical($1, 50)`. Bare UI shows the first 10.
  Extra fields and empty strings get 400. No-term input gets 200
  with `results: []`. Query text is never logged, echoed, stored
  or put in a URL.
- Slice 3: `scripts/run_eval.py`. Calls `/api/search` only. Reports
  Top-1, Top-3, MRR@50 and no-target-in-top-50 per track. Never
  prints or writes query text. `--json-out` writes case IDs, ranks,
  scores and article IDs only, and never overwrites a file.
  Smoke fixture: `tests/fixtures/fake_cases.json`.

## Tests (as of commit c798c25)
- Python: 217 tests. 9 skipped: the live SQL tests that need
  KB_FINDER_TEST_DATABASE_URL.
- tests.test_run_eval: 84.
- Web unit tests (`npm run test:unit`): 33.
- `npm run test:db`: 6.

## First lexical baseline
- One run, by the human, at 40b23d7. Depth 50.
- The numbers, cases file hash, corpus and migration hashes are in
  DECISIONS.md, "First lexical baseline result". That entry is the
  source of truth. Do not copy numbers from anywhere else.
- The results JSON from `--json-out` is kept outside the repo, on
  the project owner's machine.
- The frozen config does not change because of these numbers.
  Improvements are new, logged experiments at depth 50.

## Known limitations
- Small set: 21 positives, 4 negatives. One case moves Top-1 by
  about 5 points.
- case_021 misses on both tracks (vocabulary gap).
- case_013 and case_025 (negatives) returned close articles. Labels
  do not change after the first run. This goes to a later,
  separately versioned eval set.
- Case topics came partly from ChatGPT. See the Phase 3 provenance
  entry in DECISIONS.md.
- staffOnly text is stored but not searchable.
- `request.json()` reads the whole body before zod checks the 5000
  character limit. A body size limit is needed before hosting.
- `supabase start -x` still pulls some excluded images during
  schema setup. They do not stay running.
- No real english-parser lexeme containing `'` or `\` was found, so
  that escaping path is untested by a real lexeme.
- tsvector positions cap at 16,383 in the longest articles (no
  effect on ts_rank).
- No `seed.sql`. `supabase db reset` warns and skips seeding.
- `next dev` does not read `.env.local` in the parent process. Set
  `$env:NEXT_TELEMETRY_DISABLED = "1"` in the shell first.

## What remains
- README.md and the `npm run dev` line in AGENTS.md (drafted in
  this session, awaiting approval).
- Phase 5, semantic experiment (WORKFLOW.md section 8): cost
  protection first, pgvector, `article-v1` template, offline embed,
  `semantic` mode behind server-side gating, and a pre-registered
  decision rule committed before any semantic eval run.
- NOTES.md items: request body size limit before hosting; rename
  `vitest.config.ts` to `vitest.config.mts` (low priority); decide
  whether staffOnly is searchable, keeping in mind that some linked
  technician articles are not in the corpus.

## Unresolved human decisions
- Whether staffOnly text is searchable (currently excluded).
