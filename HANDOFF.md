# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## What was built (Phase 4, slice 1: database and ingestion)
- Local Postgres via the Supabase CLI (2.118.0), DB-only: `supabase start -x ...` (all
  non-DB services excluded; only `supabase_db_kb-finder`, Postgres 17.6, runs). DB URL
  `postgresql://postgres:postgres@127.0.0.1:54322/postgres`. `supabase/config.toml` is the
  unmodified `supabase init` default.
- Migration `supabase/migrations/20260930042716_create_articles.sql`
  (SHA-256 93903628588f6f313e33ebe130d6c0de8f0e17817602034c27289f46a7d02d59):
  - `articles` table mirroring articles.jsonl (snake_case, `source_article_id` text,
    `id` bigint identity, times timestamptz, sections/staff_only jsonb).
  - `search_vector`: STORED generated column. title A; tags B (via immutable
    `kb_join_tags`); description B; full_text C; `english`. GIN index.
    staff_only is stored but not in the vector and not indexed.
  - `kb_lexical_tsquery(q)`: OR of the query's distinct english lexemes, built in SQL;
    NULL when there are none (empty, whitespace, punctuation, stopwords only).
  - `kb_search_lexical(q, k)`: `ts_rank` (default weights, normalization 0), ties by
    `source_article_id COLLATE "C"`, 1-based rank. The API is to pass k = 50.
  - Ranking is frozen. Changes need an approved DECISIONS.md entry.
- `scripts/ingest_kb.py`: validates every record before connecting (all errors collected,
  named by sourceArticleId or line, never the value), then one-transaction upsert in
  numeric ID order that only rewrites changed rows. `--prune` is opt-in; without it
  DB-only rows are counted as `db_only`. DATABASE_URL from the environment only; non-local
  hosts need `--allow-remote`. DB errors print only class, SQLSTATE and constraint.
  Exit codes 0 ok, 1 validation, 2 usage/config, 3 database.
- Tests: `tests/test_ingest_kb.py` (35, offline, fake DB layer, sentinel checks that no
  values leak) and `tests/test_search_sql.py` (9, live DB, skipped unless
  KB_FINDER_TEST_DATABASE_URL is set; each test runs the real migration in a throwaway
  schema with search_path set to only that schema, inside a rolled-back transaction, and
  checks public is untouched).
- SPEC.md: retrieval depth fixed server-side at 50, UI shows 10, empty/no-term input
  returns 200 with results [], run_eval reports MRR@50 and a no-target-in-top-50 count.
- requirements.txt: `psycopg[binary]==3.3.6`. `.env.example` (local DATABASE_URL only).

## Results
- Ingest: input sha256 103f2b008085d9e38ece49b3e67467ae527d6fea1b8c26935e507ab16da9c1d3,
  run 1 `inserted 3109`, run 2 `unchanged 3109 db_only 0 deleted 0`, both exit 0.
- Counts: 3109 rows, 0 null search_vector, 1255 with staff_only,
  `kb_search_lexical('the and of', 50)` returns 0 rows.
- Tests: full offline suite 124/124 OK (about 8 minutes; parser tests read
  data/raw/articles). Live SQL tests 9/9 OK; unset, 9 skipped. public.articles stayed
  3109 and no kbtest_ schema remained after either run.
- No ranking was inspected on the real corpus. No eval run.

## Known limitations
- `supabase start -x` still pulls the realtime, storage-api and gotrue images during
  schema initialisation; they do not stay running.
- `supabase stop` has not been run in this session.
- The special-character test proves queries with quotes, backslashes and tsquery
  operators do not error; no english-parser input was found that yields a lexeme
  containing `'` or `\`, so that escaping path is untested by a real lexeme.
- tsvector positions cap at 16,383; positions in the longest articles are clamped
  (no effect on ts_rank).
- `seed.sql` does not exist; `supabase db reset` warns and skips seeding.

## What remains
- Human: commit slice 1.
- Slice 2: Next.js scaffold (in a temp folder outside the repo, copy in only approved
  files), POST /api/search calling `kb_search_lexical($1, 50)`, mode gating, no request
  logging, bare UI showing 10, npm lint/build/test:unit.
- Slice 3: run_eval.py (calls /api/search only; Top-1, Top-3, MRR@50,
  no-target-in-top-50 count), tests/fixtures/fake_cases.json, smoke test, README.

## Unresolved human decisions
- Whether staffOnly text is searchable (measure later; currently excluded).
