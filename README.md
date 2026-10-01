# KB Finder

KB Finder maps messy IT issue descriptions to articles in the public
Coyote OneStop Knowledge Base. It returns a ranked list of real
articles. It never writes answers, titles, IDs or links.

The candidate pool is the Technology category (ID 61) and all its
subcategories: 3,109 articles.

## Modes

- `lexical`: Postgres full-text search ("Postgres FTS baseline", not
  BM25). The default today.
- `semantic`: OpenAI `text-embedding-3-small` embeddings with exact
  cosine search in pgvector, over whole articles (template
  `article-v1`). Allowed only when `EVAL_MODES_ENABLED` is exactly
  `true`.

Semantic met the rule pre-registered in DECISIONS.md, so it becomes
the default mode when the app is hosted. That switch has not been
made yet. `DEFAULT_SEARCH_MODE` is unset (lexical), and the bare UI
still sends `mode: "lexical"` in every request.

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

Local Postgres through the Supabase CLI, database only. **Start
Supabase only from a shell that does not have `OPENAI_API_KEY`.**

```
supabase start -x gotrue,realtime,storage-api,imgproxy,kong,mailpit,postgrest,postgres-meta,studio,edge-runtime,logflare,vector
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
npm run dev
```

All search goes through `POST /api/search`. The UI shows the top 10.
The API returns at most 50. Without a key and without
`EVAL_MODES_ENABLED`, only lexical works.

## Run the eval

With the app running, from a shell without the key:

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
The key exists only in the app shell, typed in when needed, and never
in a file.

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
- `OPENAI_API_KEY` is shell-only and server-side only. It is never in
  `.env.local`, never under a `NEXT_PUBLIC_` name, and no Supabase
  component reads it.
- Errors are logged as a class and a fixed code. Provider error text
  and query text never reach a log.
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

- **Phase 7: clean UI and hosting.** Semantic becomes the default
  there. That needs `DEFAULT_SEARCH_MODE` and a change to the UI's
  hard-coded `mode`. The pre-hosting items are listed in NOTES.md.
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
