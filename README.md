# KB Finder

KB Finder maps messy IT issue descriptions to articles in the public
Coyote OneStop Knowledge Base. It returns a ranked list of real
articles. It never writes answers, titles, IDs or links.

The candidate pool is the Technology category (ID 61) and all its
subcategories: 3,109 articles.

Current mode: `lexical`, a Postgres full-text search baseline
("Postgres FTS baseline", not BM25).

## Setup

Python 3.12 in `.venv`, Node, and the Supabase CLI. Commands are for
PowerShell on Windows.

```
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
npm install
```

## Start the database

Local Postgres through the Supabase CLI, database only:

```
supabase start -x gotrue,realtime,storage-api,imgproxy,kong,mailpit,postgrest,postgres-meta,studio,edge-runtime,logflare,vector,supavisor
supabase db reset
```

## Ingest the articles

Copy `.env.example` to `.env.local`. Set `DATABASE_URL` in the shell
too (see `.env.example`), then:

```
.venv/Scripts/python scripts/ingest_kb.py
```

It reads `data/parsed/articles.jsonl`. It refuses non-local hosts
unless `--allow-remote` is given.

## Start the app

Set `$env:DATABASE_URL` (see `.env.example`), then:

```
$env:NEXT_TELEMETRY_DISABLED = "1"
npm run dev
```

All search goes through `POST /api/search`. The UI shows the top 10.
The API returns at most 50.

## Run the eval

With the app running:

```
.venv/Scripts/python scripts/run_eval.py --mode lexical --cases eval/cases.json
```

`--cases` has no default. Smoke test with the fake set:

```
.venv/Scripts/python scripts/run_eval.py --mode lexical --cases tests/fixtures/fake_cases.json
```

## Tests

```
.venv/Scripts/python -m unittest discover -s tests
npm run lint
npm run build
npm run test:unit
```

Live database tests are skipped unless
`$env:KB_FINDER_TEST_DATABASE_URL` is set.

## Privacy

- Query text is ephemeral. It is never logged, echoed, stored or put
  in a URL.
- `run_eval.py` never prints or writes query text. `--json-out`
  writes case IDs, ranks, scores and article IDs only.
- No names, IDs, emails, phone numbers or ticket data.
- Issues are typed in by hand with personal details removed.
- Planned semantic and normalized modes will send query text to
  OpenAI. The current lexical mode does not.

## Frozen baseline

Set before any eval run. Nothing here changes after seeing eval
numbers. Any change is a new, logged experiment.

- english config. Title weight A, tags and description B, full text C.
- staffOnly text is stored but not searchable.
- Query lexemes are ORed together.
- `ts_rank`, default weights, normalization 0. Ties break on
  `source_article_id` with C collation.
- Ranking lives in one SQL function, `kb_search_lexical`. Nothing
  else reimplements it.
- Depth fixed at 50 on the server. The client cannot change it.
- statement_timeout 5000 ms, pool of 5 connections.
- Corpus and migration are pinned by SHA-256 in DECISIONS.md.

## First lexical baseline

One run at commit 40b23d7, depth 50.

| Track | Top-1 | Top-3 | MRR@50 | No target in top 50 |
|---|---|---|---|---|
| A (customer_query) | 7/21 (33%) | 13/21 (62%) | 0.481 | 1/21 |
| B (technician_query) | 8/21 (38%) | 11/21 (52%) | 0.489 | 1/21 |

Details and observations: DECISIONS.md, "First lexical baseline
result".

**Small sample.** There are 21 positives. Each case moves Top-1 by
about 5 points. Differences of one or two cases mean little.

**Negatives.** The 4 negative cases are reviewed by hand, not scored.
All four returned keyword matches. case_013 got print articles.
case_025 got Troubleshooting Home Internet. Both are close to what
the student asked for. Labels do not change after the first run, so
this goes to a later, separately versioned eval set.

## How the eval set was made

- The query topics came from LLM drafts written without access to
  the KB. It is not verified what project context the topic model
  had. Some cases came from drafts written without a topic list.
  See DECISIONS.md, "Where the case topics came from".
- The project owner wrote or edited the final wording. It was frozen
  in commit 7052d5e, before any labels.
- Target labels were assigned afterward, in commit 3eecb22. The 3
  label changes are logged in DECISIONS.md.

## What comes next and why

Lexical search only matches words. case_021 misses on both tracks.
Its query wording (a laptop screen breaking) shares few words with
the article title (Student Personal Computer Hardware Repair). Next is
semantic search, at the same depth of 50. It will be compared case
by case against this baseline.

## Data note

The articles come from the public Coyote OneStop Knowledge Base.
Collection respected robots.txt with a 6-second delay. This is a
personal project, not an official university tool.
