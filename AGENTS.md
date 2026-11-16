# Project Guardrails

## Context
Retrieval tool mapping IT issue descriptions to public Coyote OneStop
Knowledge Base articles. Full spec: SPEC.md. Decisions and eval results: DECISIONS.md.
Retrieval quality comes before UI polish.

## Current Stack
- Python 3.12 (in .venv): KB collection, parsing, ingestion, offline
  article embedding, evaluation orchestration
- Next.js, React, strict TypeScript: the app and POST /api/search
- PostgreSQL via local Supabase (database plus the local transaction
  pooler); pgvector in the extensions schema; hosted app connects as
  kb_app
- Search modes: lexical, semantic, and the eval-only lexical-v2,
  semantic-v2 and hybrid. DEFAULT_SEARCH_MODE is always allowed and may only be
  lexical or semantic (unset means lexical; the hosted app sets
  semantic); other modes need EVAL_MODES_ENABLED exactly "true"
- Access gate: one shared password (a 4- or 6-digit PIN or a passphrase
  of 20+ characters), signed 7-day cookie, one global cap of 100 failed
  logins per UTC day; ACCESS_GATE=off only off Vercel (local dev and
  evals)
- Limits: 32 KiB search body cap; one global cap of 2000 valid
  searches per UTC day in kb_rate_counters, whose global rows are kept
  90 days as daily totals; RATE_LIMIT=off only off Vercel (local dev
  and evals)
- OpenAI text-embedding-3-small for the semantic and hybrid modes only
- next.config.ts must keep agentRules: false (next dev otherwise writes to AGENTS.md)

Phase 7 is complete and deployed. Planned later: query
normalization (Phase 6).
The hosted app is used by the owner and several coworkers, all behind
the one shared password.

## Commands
- py -3.12 -m venv .venv
- .venv/Scripts/python -m pip install -r requirements.txt
- .venv/Scripts/python scripts/inspect_kb.py
- .venv/Scripts/python scripts/collect_kb.py [--limit N] [--refresh-categories]
- .venv/Scripts/python scripts/parse_kb.py
- .venv/Scripts/python -m unittest discover -s tests
- supabase start -x gotrue,realtime,storage-api,imgproxy,kong,mailpit,postgrest,postgres-meta,studio,edge-runtime,logflare,vector
  (only from a shell without OPENAI_API_KEY)
- supabase db reset
- .venv/Scripts/python scripts/ingest_kb.py [--input PATH] [--prune] [--allow-remote]
  (needs $env:DATABASE_URL; see .env.example)
- .venv/Scripts/python -m unittest tests.test_ingest_kb
- .venv/Scripts/python -m unittest tests.test_search_sql
  (live DB; skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)
- .venv/Scripts/python scripts/embed_kb.py preflight
  (zero API; prints token counts, cache hits/misses, projected cost)
- .venv/Scripts/python scripts/embed_kb.py embed --paid --max-cost-usd X
  (HUMAN ONLY: paid; needs OPENAI_API_KEY in that shell; hard cap $0.50)
- .venv/Scripts/python scripts/embed_kb.py load [--allow-remote]
  (no API; needs $env:DATABASE_URL; loads the article cache)
- .venv/Scripts/python scripts/compare_eval_json.py OLD NEW
  (human runs it on real --json-out reports; agents only on synthetic ones)
- .venv/Scripts/python -m unittest tests.test_embed_kb
  (live-DB parts skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)
- .venv/Scripts/python -m unittest tests.test_semantic_sql
  (live DB; skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)
- .venv/Scripts/python -m unittest tests.test_compare_eval_json
- .venv/Scripts/python scripts/paired_eval.py BASE NEW
  (human runs it on real --json-out reports; agents only on synthetic ones)
- .venv/Scripts/python -m unittest tests.test_paired_eval
- .venv/Scripts/python -m unittest tests.test_db_privileges
  (live-DB parts skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)
- .venv/Scripts/python -m unittest tests.test_hardening_sql
  (live-DB parts skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)
- .venv/Scripts/python -m unittest tests.test_article_v2_sql
  (live-DB parts skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)
- .venv/Scripts/python scripts/db_fingerprint.py [--per-article] [--settings] [--allow-remote]
  (read-only; needs $env:DATABASE_URL; remote also needs sslmode=verify-full)
- .venv/Scripts/python -m unittest tests.test_db_fingerprint
  (live-DB part skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)
- .venv/Scripts/python scripts/run_eval.py --mode MODE --cases <path> [--base-url URL] [--allow-remote] [--json-out PATH]
  (needs the app running; agents pass only tests/fixtures/fake_cases.json)
- .venv/Scripts/python -m unittest tests.test_run_eval
- npm install
- npm run dev
  (needs $env:DATABASE_URL and $env:NEXT_TELEMETRY_DISABLED = "1";
  local dev and evals also set $env:ACCESS_GATE = "off" and
  $env:RATE_LIMIT = "off")
- npm run lint
- npm run build
- npm run test:unit
- npm run test:db
  (live DB; skipped unless $env:KB_FINDER_TEST_DATABASE_URL is set)

Always run Python through .venv/Scripts/python. Never use bare
python or python3 (python3 on this machine is 3.9).
Add commands only after they exist. Do not invent command names.

## Retrieval Rules
- All production retrieval goes through POST /api/search.
- run_eval.py must call /api/search. Never reimplement ranking in Python.
- Never invent article titles, IDs, or URLs.
- Do not add normalization, chunking, or reranking until
  that experiment begins. Semantic exists (Phase 5); its frozen
  configuration is in DECISIONS.md and does not change without a new,
  logged experiment.
- Hybrid exists as the eval-only mode hybrid (hybrid-rrf-v1). Its
  frozen configuration and decision rule are in DECISIONS.md and do
  not change without a new, logged experiment. Its fusion in
  lib/search/hybrid.ts is the only ranking code outside SQL; do not
  add another.
- Experimental modes must be gated server-side.

## Human-Owned Files
- Never create, edit, or delete eval/**, data/samples/urls.txt,
  data/samples/category_urls.txt, or NOTES.md.
- Never read eval/** unless the human's own message in this session
  says it is the labeling session. Reading includes git show, git diff
  and git log -p on those paths. Use the schema in SPEC.md instead.

## Data Handling
- Public KB article content may be stored.
- Issue/ticket text must stay ephemeral. Never persist or log
  request bodies containing issue text.
- Do not store names, IDs, emails, phone numbers, or ticket data.
- Semantic, hybrid and normalized modes send query text to OpenAI.
  This is an accepted decision: issues are typed in manually
  with names, IDs, emails, and phone numbers removed.
- Query embeddings are ephemeral: never write query text, its hash
  or its vector to disk, cache, logs or the database.
- Exception, owner decision 2026-11-15: the eval v2 pilot is
  de-identified ticket text. It is stored only outside this repo.
  Agents never read, commit or print it, and refer to rows by row_id
  only. During scoring it is sent only to the OpenAI embeddings API,
  with the local eval key (OPENAI_API_KEY set in the local dev shell,
  never the hosted Vercel key), and never to a chat model unless a
  later logged decision in DECISIONS.md allows it. The app still
  stores and logs nothing from these requests.

## Paid Calls and Keys
- Agents never run embed_kb.py embed --paid, never run a real eval
  (any run_eval.py against eval/cases.json, any mode), and never set
  OPENAI_API_KEY in any shell or file.
- OPENAI_API_KEY is shell-only and server-side only: never in
  .env.local or any file, never under a NEXT_PUBLIC_ name.
  Vercel carve-out: the hosted app reads it from a server-side Vercel
  environment variable, Production only, marked Sensitive, never in a
  file. Only the human sets it; agents never set or read it.
- Start Supabase only from a shell without OPENAI_API_KEY.
- All tests run offline with fakes; no test may build a real OpenAI
  client.

## Verification
Before reporting work complete:
- run the relevant command (except run_eval.py on real cases, which
  the human runs; agents run it only with
  --cases tests/fixtures/fake_cases.json)
- report failures
- show the actual diff
- do not claim success from code inspection alone

## Commits
- Never commit or push without the human's explicit approval in the
  current session, given for that specific commit or push.
- Never add Co-Authored-By trailers, "Generated with" lines, or any
  agent or model name to commit messages, PR text, or git author fields.
  Never change git user.name or user.email.
- Never use git commit --no-verify or -n, git push --force,
  git commit --amend, git rebase, or git reset.