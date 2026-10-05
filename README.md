# KB Finder

KB Finder maps messy IT issue descriptions to articles in the public
Coyote OneStop Knowledge Base. It returns a ranked list of real
articles. It never writes answers, titles, IDs or links.

The candidate pool is the Technology category (ID 61) and all its
subcategories: 3,109 articles.

## Modes

- `lexical`: Postgres full-text search ("Postgres FTS baseline", not
  BM25).
- `semantic`: OpenAI `text-embedding-3-small` embeddings with exact
  cosine search in pgvector, over whole articles (template
  `article-v1`).

`DEFAULT_SEARCH_MODE` picks the default mode, which is always allowed.
Unset means `lexical`. Any other mode is allowed only when
`EVAL_MODES_ENABLED` is exactly `true`.

Semantic met the rule pre-registered in DECISIONS.md, so it is the
hosted default: the hosted app sets `DEFAULT_SEARCH_MODE=semantic` and
leaves `EVAL_MODES_ENABLED` unset. The UI reads the same setting on
the server for each page request and sends that mode, so it always
sends an allowed one. A `DEFAULT_SEARCH_MODE` that names no known mode
makes the page show a configuration error and every search a 500.

## Results

Same 21 positive cases, same labels, same `run_eval.py`, depth 50.
Numbers are copied from DECISIONS.md ("First lexical baseline result"
and "First semantic eval result"), which is the source of truth.

| Mode | Track | Top-1 | Top-3 | MRR@50 | No target in top 50 |
|---|---|---|---|---|---|
| lexical | A (customer_query) | 7/21 (33%) | 13/21 (62%) | 0.481 | 1/21 |
| lexical | B (technician_query) | 8/21 (38%) | 11/21 (52%) | 0.489 | 1/21 |
| semantic | A (customer_query) | 12/21 (57%) | 16/21 (76%) | 0.690 | 0/21 |
| semantic | B (technician_query) | 6/21 (29%) | 16/21 (76%) | 0.542 | 0/21 |

The decision rule, frozen before any semantic run: semantic becomes
the default only if Track A Top-3 >= 16/21 and Track B Top-3 >= 11/21
(aggregate counts). Both held.

**Read these numbers with care.**
- **Small sample.** There are 21 positives. One case moves a metric
  by about 5 points. Differences of one or two cases mean little.
- **Track A met the threshold exactly.** 16/21 against a bar of 16.
  One case the other way would have failed the rule.
- **Track B Top-1 fell from 8 to 6.** Cases 001, 008, 010 and 016
  moved from rank 1 to rank 2 or 3. Short technician wording that
  matches a title still favors lexical.
- **The modes do not see the same fields.** Lexical indexes title,
  tags, description and full text. Semantic embeds title and full text
  only. The gap is not only the retrieval method.
- **case_021 is fixed.** Lexical missed it on both tracks (a
  vocabulary gap: "laptop screen broke" against "Student Personal
  Computer Hardware Repair"). Semantic ranks it 3 (Track A) and 4
  (Track B).
- **case_012 Track B regressed** from rank 6 to 39, while its Track A
  improved from 29 to 5.
- **Negatives.** The 4 negative cases are reviewed by hand, not
  scored. Semantic top scores for them are 0.33 to 0.62, overlapping
  real matches, so a score cutoff would not work as abstention.
- **One scored run.** Query embeddings are not cached, so a second
  semantic run can differ slightly. The first complete run is the
  scored one.

## Setup

Python 3.12 in `.venv`, Node, and the Supabase CLI. Commands are for
PowerShell on Windows.

```
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
npm install
```

## Start the database

Local Postgres through the Supabase CLI: the database and the local
transaction pooler (Supavisor, port 54329), nothing else. The pooler
is there to test the hosted path as `kb_app`; scripts, tests and local
dev use the direct port 54322 as `postgres`. **Start Supabase only from
a shell that does not have `OPENAI_API_KEY`.**

```
supabase start -x gotrue,realtime,storage-api,imgproxy,kong,mailpit,postgrest,postgres-meta,studio,edge-runtime,logflare,vector
supabase db reset
```

`supabase db reset` applies every migration, including the Phase 7
ones: the Data API lockdown, the `kb_app` role with the
`kb_rate_counters` table, and the pinned `search_path` on the `kb_`
functions. `kb_app` is created `NOLOGIN` with no password. To use it
through the pooler, set LOGIN and a password by hand and keep the
password out of every file (see `.env.example`).

## Check a database (read-only)

```
.venv/Scripts/python scripts/db_fingerprint.py [--per-article] [--settings] [--allow-remote]
```

It prints counts, config fields, versions and SHA-256 hashes of the
articles and embedding vectors, in one read-only transaction that is
always rolled back, so a local and a hosted database can be compared
with a plain diff. `--settings` adds server settings such as the
`auto_explain` ones. It needs `$env:DATABASE_URL`, refuses non-local
hosts without `--allow-remote`, and a remote URL must also set
`sslmode=verify-full`. It never prints the connection string or any
article text.

## Ingest the articles

Copy `.env.example` to `.env.local`. Set `DATABASE_URL` in the shell
too (see `.env.example`), then:

```
.venv/Scripts/python scripts/ingest_kb.py
```

It reads `data/parsed/articles.jsonl`. It refuses non-local hosts
unless `--allow-remote` is given.

## Load the article embeddings (no API call)

The article embeddings live in a gitignored cache,
`cache/article_embeddings/`, pinned by sha256 in DECISIONS.md. After
a `supabase db reset` and ingest, load them with no API call:

```
.venv/Scripts/python scripts/embed_kb.py load
```

It runs in one transaction and finishes by running the semantic search
function once. If any article is missing, stale or mismatched, the
load rolls back.

## Start the app

Set `$env:DATABASE_URL` (see `.env.example`), then:

```
$env:NEXT_TELEMETRY_DISABLED = "1"
$env:ACCESS_GATE = "off"
$env:RATE_LIMIT = "off"
npm run dev
```

`ACCESS_GATE=off` and `RATE_LIMIT=off` are for local dev and eval runs
only. Without them the gate needs `ACCESS_PASSWORD` and
`GATE_COOKIE_SECRET`, and every valid search counts against the daily
cap. Each flag works only when it is exactly `off` and `VERCEL` is
unset; on Vercel either one makes every search a 500.

All search goes through `POST /api/search`. The UI shows the top 10.
The API returns at most 50. With `DEFAULT_SEARCH_MODE` unset and
without `EVAL_MODES_ENABLED`, only lexical works.

## Hosted controls

The hosted app is private: the project owner plus at most 2 or 3
trusted testers. In brief:

- **Password gate.** One shared password (`ACCESS_PASSWORD`: exactly
  4 ASCII digits, or at least 20 characters). `POST /api/gate` checks
  it and sets a signed, HttpOnly, Secure, SameSite=Lax cookie that
  lasts 7 days. The MAC
  covers the expiry and sha256 of the password, keyed by
  `GATE_COOKIE_SECRET` (at least 32 bytes), so rotating either one
  revokes every cookie. Pages without a valid cookie redirect to
  `/gate`; `/api/search` checks the cookie itself and answers 401. A
  missing or invalid password or secret fails closed.
- **Failed-login cap.** At most 10 failed logins per UTC day across
  all clients, in `kb_rate_counters` (kind `gate_fail_global_day`,
  key `all`, no IP stored). Each attempt reserves a slot before the
  password is compared, and a correct one gives it back. Attempt 11
  of a day is a 429 and is never compared. Failing 10 times locks out
  new logins until the next UTC day; existing cookies keep working.
- **Body cap.** `/api/search` reads at most 32 KiB (32,768 bytes)
  before parsing; a larger body is a 413. `/api/gate` reads at most
  1 KiB.
- **Daily cap.** One global counter: 200 valid, allowed searches per
  UTC day across both modes. Search 201 of a day is a 429. Invalid
  and unauthorized requests do not count; a search that fails after
  the cap check does. The counter lives in `kb_rate_counters`.
- **`kb_app` role.** The hosted app connects as `kb_app` through the
  transaction pooler. It can read the KB tables, run the search
  functions and their two helpers, and read and write the counter,
  nothing else. `statement_timeout` is 5000 ms on the role itself.
  RLS is on for every table, with
  policies only for `kb_app`; `anon`, `authenticated` and PUBLIC have
  no privileges on the KB objects.
- **TLS.** When `VERCEL` is set, the server verifies the database
  certificate chain against `DATABASE_CA_PEM` (the Supabase database
  CA) and checks the hostname. A missing or invalid CA, or any query
  string in `DATABASE_URL`, makes every search a 500.
- **Semantic config check.** Before the first semantic query is
  embedded, the server checks that the stored `embedding_configs` row
  matches `config/embedding/article-v1.json` field for field. Only a
  pass is remembered, once per server process. A mismatch is a 500
  and spends no OpenAI call.

## Run the eval

With the app running (`ACCESS_GATE=off` and `RATE_LIMIT=off` in the
app shell; `run_eval.py` sends no cookie, and a real run would use up
the day's cap), from a shell without the key:

```
.venv/Scripts/python scripts/run_eval.py --mode lexical --cases eval/cases.json
```

`--cases` has no default. Smoke test with the fake set:

```
.venv/Scripts/python scripts/run_eval.py --mode lexical --cases tests/fixtures/fake_cases.json
```

Compare two `--json-out` reports case by case (rank, article ID and
exact score):

```
.venv/Scripts/python scripts/compare_eval_json.py OLD.json NEW.json
```

## Paid steps (project owner only)

Agents never run these and never set `OPENAI_API_KEY`. Use a dedicated
OpenAI project with prepaid credit, auto-reload off and a spend limit.
Locally the key exists only in the app shell, typed in when needed,
and never in a file. The hosted app uses a restricted key of its own,
set only in Vercel (see Privacy).

### Article embed (once)

1. Plan with no API call:
   `.venv/Scripts/python scripts/embed_kb.py preflight`
   It prints token counts, cache hits and misses, and projected cost.
2. In a fresh shell that will not start Supabase:
   ```
   $env:OPENAI_API_KEY = Read-Host -MaskInput "OpenAI key"
   .venv/Scripts/python scripts/embed_kb.py embed --paid --max-cost-usd 0.05
   Remove-Item Env:OPENAI_API_KEY
   ```
   `--max-cost-usd` is required, with a hard cap of $0.50 and 2,000,000
   tokens. The run stops if the billed tokens pass either limit. A
   stopped run keeps a journal, so rerunning never re-bills finished
   batches.
3. Load into Postgres (no key): `.venv/Scripts/python scripts/embed_kb.py load`

**Cost:** the full article embed was 544,549 tokens, about $0.011
(DECISIONS.md, "Phase 5 article embed"). Each semantic query is one
small embedding call.

### Semantic eval

- **Shell 1, database, no key:** `supabase start ...` as above.
- **Shell 2, app, the only shell with the key:**
  ```
  $env:DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
  $env:NEXT_TELEMETRY_DISABLED = "1"
  $env:EVAL_MODES_ENABLED = "true"
  $env:ACCESS_GATE = "off"
  $env:RATE_LIMIT = "off"
  $env:OPENAI_API_KEY = Read-Host -MaskInput "OpenAI key"
  npm run dev
  ```
- **Shell 3, eval runner, no key:**
  ```
  .venv/Scripts/python scripts/run_eval.py --mode semantic --cases tests/fixtures/fake_cases.json
  .venv/Scripts/python scripts/run_eval.py --mode semantic --cases eval/cases.json --json-out <new file>
  ```
  The fake-case run is a smoke test, not an eval run.
- **Afterwards:** stop the app and close shell 2.

## Tests

```
.venv/Scripts/python -m unittest discover -s tests
npm run lint
npm run build
npm run test:unit
npm run test:db
```

Live database tests are skipped unless
`$env:KB_FINDER_TEST_DATABASE_URL` is set. No test calls OpenAI: web
tests block real `fetch`, and the embedding tests block the network
and never build a real client.

## Privacy

- Query text is ephemeral. It is never logged, echoed, stored or put
  in a URL.
- Query embeddings are ephemeral too. The query vector and any query
  hash are never written to disk, cache, logs or the database.
- Semantic mode sends query text to OpenAI. Issues are typed in by
  hand with personal details removed. Lexical mode sends nothing to
  OpenAI.
- `OPENAI_API_KEY` is server-side only. Locally it lives only in the
  app shell. On Vercel it is a Production-only environment variable
  marked Sensitive. It is never in `.env.local` or any other file,
  never under a `NEXT_PUBLIC_` name, and no Supabase component reads
  it.
- Errors are logged as a class and a fixed code. Provider error text
  and query text never reach a log.
- Every error body is fixed and never echoes the request: 400
  `{"error":"Invalid request"}`, 401 `{"error":"Unauthorized"}`, 413
  `{"error":"Request too large"}`, 429 `{"error":"Too many requests"}`,
  500 `{"error":"Search failed"}`. The UI maps each status to its own
  fixed message and never shows response text.
- The daily cap stores one global counter per UTC day (key `all`),
  never an IP address or anything about the request. Windows older
  than one day are deleted.
- The gate password is never logged, echoed or stored; the cookie
  holds only an expiry and a MAC.
- `run_eval.py` never prints or writes query text. `--json-out`
  writes case IDs, ranks, scores and article IDs only.
- No names, IDs, emails, phone numbers or ticket data.

## Frozen configurations

Set before any eval run of that mode. Nothing here changes after
seeing eval numbers. Any change is a new, logged experiment.

**Lexical** (DECISIONS.md, Phase 4):
- english config. Title weight A, tags and description B, full text C.
- staffOnly text is stored but not searchable.
- Query lexemes are ORed together.
- `ts_rank`, default weights, normalization 0. Ties break on
  `source_article_id` with C collation.
- Ranking lives in one SQL function, `kb_search_lexical`.

**Semantic** (DECISIONS.md, "Phase 5 semantic-v1 configuration"):
- `text-embedding-3-small`, 1536 dimensions.
- `article-v1` is exactly `"Title: {title}\n\n{fullText}"`, with no
  staffOnly text.
- Articles over 8,000 cl100k_base tokens are cut to their first 8,000
  token IDs. One article (9431) is cut.
- Exact cosine search, no ANN index. Score is cosine similarity, not
  a probability. Ties break on `source_article_id` with C collation.
- Ranking lives in one SQL function, `kb_search_semantic`. It fails
  loudly on an incomplete, stale or mismatched embedding set.

**Both:**
- Depth fixed at 50 on the server. The client cannot change it.
- statement_timeout 5000 ms, pool of 5 connections.
- Corpus, migrations, embedding config and article cache are pinned by
  SHA-256 in DECISIONS.md.

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

- **Phase 7: clean UI and hosting.** The hosting code is done
  (default mode, gate, caps, `kb_app`, TLS). Deploying to Vercel and
  hosted Supabase, checking the hosted database against the local
  fingerprint, and the UI pass are still to come.
- **Phase 6: query normalization.** A stretch goal that starts only
  after hosting.
- **Separate, logged experiments.** Hybrid retrieval, reranking and a
  new, separately versioned eval set (case_013 and case_025 returned
  articles close to what the student asked for). None of them
  changes either frozen configuration.

## Data note

The articles come from the public Coyote OneStop Knowledge Base.
Collection respected robots.txt with a 6-second delay. This is a
personal project, not an official university tool.
