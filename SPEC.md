# KB Finder Spec

## Purpose
KB Finder maps messy IT issue descriptions to the right public
Coyote OneStop Knowledge Base article. It returns a ranked list of
real articles. It never generates answers, titles, IDs, or links.

Personal project. Not an official university tool.

## Scope
- Source: the public Coyote OneStop Knowledge Base, Technology
  category (ID 61) and all its subcategories: 3,109 articles as of
  the first full collection.
- Collection: plain HTTP, robots.txt respected, descriptive
  User-Agent, delay = max(6 seconds, robots.txt Crawl-delay)
  (Crawl-delay is currently 5).
- Discovery: recursive walk of category pages starting at
  /TDClient/33/Portal/KB/Category/61/Technology. Only the main
  article listing and subcategory links are used; sidebars and
  links inside article bodies are never followed.
- Parse source: the normal article page. Body selector:
  #ctl00_ctl00_cpContent_cpContent_divBody
- Print route (/TDClient/33/Portal/KB/PrintArticle?ID=<id>) exists
  but is not used for parsing (it has no meta tags).

## Stack
- Python 3.12 (.venv): KB inspection, collection, parsing,
  ingestion, offline article embedding, evaluation orchestration.
- Next.js, React, strict TypeScript: app and /api/search.
- PostgreSQL from the first retrieval slice. Local via the
  Supabase CLI; hosted on Supabase.
- pgvector (in the `extensions` schema): semantic retrieval, since
  Phase 5.
- OpenAI text-embedding-3-small: article embeddings (offline, once)
  and query embeddings (per semantic request).
- No SQLite, no FTS5.

## Article model

Exact field shapes are defined in scripts/parse_kb.py.

```ts
type Article = {
  id: string;                // internal ID (assigned at ingestion; not in articles.jsonl)
  sourceArticleId: string;   // KB article ID
  title: string;
  url: string;
  category: string | null;   // meta article:section
  tags: string[];
  description: string | null;   // og:description
  publishedTime: string | null;
  modifiedTime: string | null;
  fullText: string;          // see fullText rules below
  question: string | null;   // null when the section is missing (never invented)
  answer: string | null;
  issue: string | null;
  environment: string | null;
  cause: string | null;
  resolution: string | null;
  // all sections, in order; heading is null for content before the first
  // heading (preamble) or after a footer
  sections: { heading: string | null; text: string }[];
  // internal technician content, kept separate
  staffOnly: { text: string; links: { text: string; href: string }[] } | null;
  audienceLabels: string[];  // e.g. "USD STAFF ONLY"; content stays searchable
  // original footer text before the standard line is removed (staff-only block
  // already excluded); null if no footer. For inspection only. Leftover
  // non-standard footer lines are also in fullText.
  footer: string | null;
  parserVersion: string;     // parser revision that produced the record (currently "2")
  warnings: string[];        // parser flags, e.g. footer_nonstandard, footer_unclassified,
                             // staff_only_unmatched; [] if none
};
```

Article structures seen:
- Issue/Question / Environment / Cause / Resolution
  (Cause sometimes missing). "Issue/Question" maps to issue.
- Question / Products or Services Involved / Instructions /
  Need More Help?
- Question / Answer (a few articles)
- Other headings (e.g. Description / FAQs): kept in sections only.

fullText rules:
- Main article body only: no navigation, Related Articles, or
  feedback widgets. Headings are included.
- Internal technician content ("ITS STAFF ONLY" boxes, top-level
  "Internal Staff Only" panels) is excluded and stored in staffOnly.
- Department audience labels ("USD/HR/FINANCE/EPC STAFF ONLY"): only
  the label line is removed; the content stays in fullText.
- The standard service-desk contact line is removed; all other
  footer content stays in fullText.
- Inline tags add no spaces; block elements separate text.
- Zero-width characters (U+200B-U+200D, U+2060, U+FEFF) are removed.
- Deterministic: same HTML in, same output out.

## Storage

Postgres. The two retrieval migrations are pinned by sha256 in
DECISIONS.md and never edited. Four Phase 7 migrations follow them
and change no function body or ranking: the Data API lockdown (no
privileges for `anon`, `authenticated` or PUBLIC, RLS on every
table), the `kb_app` role and `kb_rate_counters` table (RLS policies
only for `kb_app`), a pinned `search_path` on the five `kb_`
functions, and the `gate_fail_global_day` kind added to the two
`kb_rate_counters` CHECK constraints (no grant changes).

- `articles`: one row per article. `source_article_id` is unique and
  is the durable key. The internal `id` is an identity column that a
  clean `supabase db reset` regenerates, so nothing references it.
  `search_vector` is a stored generated column (lexical only).
- `embedding_configs`: one row per embedding configuration.
  `config_sha256` (primary key) is the sha256 of the canonical bytes
  of `config/embedding/article-v1.json`; also `config` (jsonb),
  `model`, `dimensions` (must be 1536), `template_version`.
- `article_embeddings`: one row per (config, article), primary key
  `(config_sha256, source_article_id)`.
  - `source_article_id` references `articles(source_article_id)`
    ON DELETE RESTRICT: `ingest_kb.py --prune` of an embedded
    article fails loudly until that config's embeddings are removed.
  - `config_sha256` references `embedding_configs` ON DELETE CASCADE.
  - `source_text_sha256` (sha256 of the article-v1 text the vector
    was made from), `input_kind` (`text` or `tokens`),
    `input_sha256` (of exactly what was sent), `input_tokens`,
    `truncated` (true exactly when `input_kind` is `tokens`),
    `embedding vector(1536)` (non-zero), `loaded_at`.
  - No vector index: search is exact.
- After `supabase db reset`, run `ingest_kb.py`, then
  `embed_kb.py load` to reload embeddings from the article cache with
  no API call.

## Search API

`POST /api/search` is the only production retrieval path.
run_eval.py calls it. Ranking is never reimplemented elsewhere.

Request:

```ts
const SearchRequestSchema = z.object({
  query: z.string().min(1).max(5000),
  mode: z.enum(["lexical", "semantic"]),
}).strict(); // extra request fields (e.g. limit) are rejected with 400
```

A schema-valid mode must still pass the server-side gate (below).

Retrieval depth is fixed server-side at 50 for every mode: the
response holds at most 50 results. The client cannot choose or
increase it. It does not change after seeing evaluation results.
The UI displays only the first 10.

An empty query string ("") is rejected with 400 in every mode.

- `lexical`: input with no searchable terms (whitespace-only,
  punctuation-only or stopword-only) returns 200 with results [].
- `semantic`: there is no such rule. Any schema-valid query is
  embedded and searched, and the response holds up to 50 results
  (no score threshold, no abstention).

Response:

```ts
type SearchResponse = {
  mode: string;
  results: {
    rank: number;            // 1-based
    sourceArticleId: string;
    title: string;
    url: string;
    category?: string;
    score: number;           // mode-specific; not a probability
  }[];                       // at most 50, ordered by rank
};
```

Modes:
- `lexical`: Postgres full-text search ("Postgres FTS baseline",
  not BM25). Score is `ts_rank`. Ranking lives only in
  `kb_search_lexical`. Frozen configuration: DECISIONS.md, Phase 4.
- `semantic`: text-embedding-3-small + pgvector over whole articles.
  Frozen configuration: DECISIONS.md, "Phase 5 semantic-v1
  configuration".
- Planned, not built: `normalized_lexical`, `normalized_semantic`
  (query rewritten by the normalization model, prompt
  `normalize-v1`, then searched). Each is added only when its
  experiment starts.

Semantic mode:
- Article text is template `article-v1`, exactly
  `"Title: {title}\n\n{fullText}"` with LF newlines. No tags,
  description, category, URL, staffOnly or separate section fields.
  Lexical indexes title, tags, description and full text, so the two
  modes do not see identical fields.
- Model `text-embedding-3-small`, `dimensions: 1536` sent on every
  article and query call. The config is
  `config/embedding/article-v1.json`; its sha256 is the config
  identity, and both the embed script and the server read it from
  that file.
- Truncation: article text is tokenized with `cl100k_base` (the
  encoding file is pinned by sha256 in the config). An article that
  fits in 8,000 tokens is sent as its string; a longer one is sent as
  its first 8,000 token IDs. One article (9431) is truncated. One
  embedding request never mixes strings and token arrays.
- The query is embedded as typed, with no template
  (`raw-query-string-v1`).
- Ranking: exact cosine search with pgvector, no ANN index, in one
  SQL function, `kb_search_semantic`. Score = 1 - cosine distance,
  i.e. cosine similarity in [-1, 1]. It is not a probability.
  Order: score descending, then `source_article_id COLLATE "C"`.
- The server passes its config sha256 to `kb_search_semantic`, which
  fails loudly instead of searching a partial, stale or mismatched
  set:
  - KBF01: k outside 1..50.
  - KBF02: config not loaded.
  - KBF03: query vector not 1536-d, or all zeros.
  - KBF04: an article has no embedding for the config, or the counts
    differ.
  - KBF05: an embedding was made from text that no longer matches its
    article.
- Any failure, whether a KBF error, an OpenAI error, a missing key or
  an invalid config, returns 500 with the fixed body
  `{"error":"Search failed"}`. The log line is the error class plus a
  fixed code (e.g. `DatabaseError KBF04`,
  `EmbeddingError OPENAI_HTTP_429`), never provider text or query
  text.

Mode gating (server-side):
- `DEFAULT_SEARCH_MODE` is always allowed. Unset means `lexical`.
  The hosted app sets it to `semantic`. A value that names no known
  mode is a config error (500 below).
- `EVAL_MODES_ENABLED` exactly `"true"` unlocks the other modes.
  Any other value, including `"TRUE"` or `"1"`, does not.
- Any other mode is rejected with 400.
- The UI resolves `DEFAULT_SEARCH_MODE` the same way, on the server
  per page request, and sends that mode.

Request flow, in order (`lib/search/handler.ts`). The first step that
fails answers the request; every body is fixed, sent with
`Cache-Control: no-store`, and never echoes the request.

1. Config checks. `DEFAULT_SEARCH_MODE` names no known mode, or
   `RATE_LIMIT=off` while `VERCEL` is set: 500
   `{"error":"Search failed"}`.
2. Access gate, from the Cookie header only, before the body is read.
   A misconfigured gate (`ACCESS_GATE=off` while `VERCEL` is set,
   `ACCESS_PASSWORD` missing or neither exactly 4 ASCII digits nor
   at least 20 characters,
   `GATE_COOKIE_SECRET` missing or under 32 bytes) is 500
   `{"error":"Search failed"}`. No valid `kbf_gate` cookie: 401
   `{"error":"Unauthorized"}`. `ACCESS_GATE` exactly `off` with
   `VERCEL` unset skips the gate (local dev and evals only).
3. Body cap. At most 32,768 bytes are read; a larger
   `Content-Length` or stream is 413 `{"error":"Request too large"}`.
   A malformed `Content-Length`, invalid UTF-8 or invalid JSON is 400
   `{"error":"Invalid request"}`.
4. Schema, then mode gating: 400 `{"error":"Invalid request"}`.
5. Daily cap, only for valid, allowed searches: one global counter,
   200 per UTC day across both modes. Search 201 of a day is 429
   `{"error":"Too many requests"}`. A limiter error is 500
   `{"error":"Search failed"}` (fails closed). `RATE_LIMIT` exactly
   `off` with `VERCEL` unset skips this step (local dev and evals
   only). A search that passes this step counts even if retrieval
   then fails.
6. Semantic only: the KBF02 config check (below), before the query is
   embedded. A mismatch is 500 `{"error":"Search failed"}`.
7. Retrieval. Any failure is 500 `{"error":"Search failed"}`.

Database connection (`lib/db.ts`): a pool of 5,
`statement_timeout` 5000 ms. Locally (`VERCEL` unset) there is no
TLS. Hosted (`VERCEL` set), `DATABASE_URL` must be a postgres URL
with no query string, and TLS verifies the chain against
`DATABASE_CA_PEM` (the Supabase database CA) and the hostname. A
missing or invalid CA or URL is a 500 on every search. The hosted app
connects as `kb_app` through the transaction pooler.

Access gate login: `POST /api/gate` with `{"password": string}`
(body capped at 1 KiB). `ACCESS_PASSWORD` is either a PIN of exactly
4 ASCII digits or a passphrase of at least 20 characters; anything
else is a misconfigured gate. Brute force is bounded by a global
failed-login cap, not by the password's length. In order:

1. Config checks. A misconfigured gate, or `RATE_LIMIT=off` while
   `VERCEL` is set: 500 `{"error":"Gate unavailable"}`.
2. Body. Oversized: 413 `{"error":"Request too large"}`. Invalid
   JSON or schema: 400 `{"error":"Invalid request"}`. Neither counts
   against the cap.
3. `ACCESS_GATE` exactly `off` with `VERCEL` unset: 204, no cookie.
4. Failed-login cap: 10 per UTC day across all clients
   (`GATE_FAIL_CAP`). Every attempt first reserves a slot atomically
   in `kb_rate_counters` (kind `gate_fail_global_day`, key `all`),
   before the password is compared, so concurrent guesses cannot get
   past the cap. Attempt 11 of a day is 429
   `{"error":"Too many attempts"}` and the password is never
   compared, even a correct one. A reserve error is 500
   `{"error":"Gate unavailable"}` (fails closed). `RATE_LIMIT`
   exactly `off` with `VERCEL` unset skips this step.
5. Compare (constant-time, both sides hashed). Wrong: 401
   `{"error":"Unauthorized"}`, and the slot stays counted.
6. Correct: the slot is released (decremented, or the row deleted at
   count 1), then 204 with the cookie `kbf_gate=v1.<exp>.<mac>`
   (HttpOnly, Secure, SameSite=Lax, Path=/, 7 days), where the MAC is
   HMAC-SHA256 keyed by `GATE_COOKIE_SECRET` over the expiry and
   sha256 of the password. A release error is logged and the login
   still succeeds; that attempt stays counted.

Every response body is fixed and sent with `Cache-Control: no-store`.
Anyone can lock out new logins until the next UTC day by failing 10
times; devices already holding a valid cookie keep working. Page
requests without a valid cookie redirect to `/gate`; `/api/search`
checks the cookie itself and is the real boundary.

KBF02 server-side check (`lib/search/semantic.ts`):
`kb_search_semantic` raises KBF02 only when no row exists for the
config sha256. Before embedding, the server also reads that
`embedding_configs` row and requires exactly one row whose `config`
(compared in canonical JSON form), `model`, `dimensions` and
`template_version` equal `config/embedding/article-v1.json`.
Otherwise it throws `ConfigMismatchError` and logs
`ConfigMismatchError KBF02_CONFIG_MISMATCH`. Only a pass is
remembered, per server process, so a failed check is retried on the
next request.

## Data handling
- Public KB article content may be stored.
- Issue text is ephemeral. Request bodies are never logged or
  stored.
- Query embeddings are ephemeral. Query text, its hash and its
  vector are never written to disk, cache, logs or the database.
- No names, IDs, emails, phone numbers, or ticket data.
- Semantic and normalized modes send query text to OpenAI.
  Issues are typed in manually with personal details removed.
- `OPENAI_API_KEY` is set locally only in the shell that runs the app
  (or the paid article embed), never in `.env.local` or any other file
  and never under a `NEXT_PUBLIC_` name.
  - Vercel carve-out: the hosted app reads it from a server-side
    Vercel environment variable, Production only, marked Sensitive,
    never in a file. Only the project owner sets it; agents never set
    or read it.
  - The server reads it only at request time, in one server-only
    module.
  - Supabase is never started from a shell that holds it, and
    `supabase/config.toml` does not reference it.
- `ACCESS_PASSWORD` and `GATE_COOKIE_SECRET` are secrets: set only in
  Vercel's environment settings, never in a file, and read only in
  `lib/gate/cookie.ts`. The password is never logged, echoed or
  stored; the cookie holds only an expiry and a MAC.
- `kb_rate_counters` holds the two global caps: one row per
  (kind, key, UTC day window) with a count. The app uses only two
  global kinds, both keyed `all` (the CHECK requires `all` for
  exactly these two): `search_global_day` (the daily search cap) and
  `gate_fail_global_day` (the failed-login cap). No IP address, IP
  hash, password or anything else from the request is stored. Each
  search cap check and each login reservation deletes windows, of
  any kind, that started more than one day before the request. The
  table's CHECK also allows per-IP kinds (`search_ip_10m`, `search_ip_day`, `gate_ip_10m`, keyed by a 64-hex
  HMAC), which the app does not use.
- Error and limit logs are fixed strings plus an error class and
  code. Request bodies, query text and the password never reach a
  log.

## Evaluation

Real cases live in `eval/cases.json` (human-owned; agents never
read it outside the labeling session). The schema is documented
here so agents never need the real file.

Schema: a JSON array of cases.

| Field | Type | Meaning |
|---|---|---|
| id | string | Case ID |
| customer_query | string | How a stressed student would type it |
| technician_query | string | Short technician wording |
| target_article_ids | string[] | Correct sourceArticleIds; empty for negatives |
| has_match | boolean | false for issues the KB should not cover |

Fake example (not a real case):

```json
[
  {
    "id": "case_fake_001",
    "customer_query": "my laptop keeps saying the thing is wrong when i log in",
    "technician_query": "login error on laptop",
    "target_article_ids": ["00001"],
    "has_match": true
  },
  {
    "id": "case_fake_002",
    "customer_query": "how do i fix my car radio",
    "technician_query": "car radio repair",
    "target_article_ids": [],
    "has_match": false
  }
]
```

The agent smoke-test fixture `tests/fixtures/fake_cases.json`
mirrors this example.

run_eval.py:
- `--mode` and `--cases <path>` are required. No default for
  `--cases`.
- Calls /api/search only. Simple asserts on the response shape.
- Logs the SHA-256 of the cases file with every run.
- Reports Track A (customer_query) and Track B
  (technician_query) separately.
- Uses all returned rows (up to 50).
- Positive cases: Top-1, Top-3, MRR@50, with raw counts
  (e.g. 14/20 (70%)). A hit means any ID in target_article_ids
  appears. MRR@50: a case with no target in the returned top 50
  contributes 0 reciprocal rank. Always labeled MRR@50, never
  plain "MRR".
- Also reports the raw count of positive cases with no target
  anywhere in the top 50, as X / <number of positives>.
- Negative cases: top article, top score, ranked list, for
  qualitative review. No precision/recall until an abstention
  mechanism exists.

Caches (under `cache/`, outside eval/, gitignored, never committed):
- `cache/article_embeddings/<first 16 hex of config sha256>.jsonl`:
  the article embedding cache, the only embedding cache.
  - Line 1 is a header holding the config. Then one row per article,
    sorted by numeric sourceArticleId, as canonical JSON.
  - Each row holds source_text_sha256, input_kind, input_sha256,
    input_tokens, truncated and the vector as base64 little-endian
    float32. No article text, no timestamps.
  - Written only by `embed_kb.py embed`, through an fsynced journal
    and an atomic replace, so an interrupted run never re-bills
    finished batches.
  - Its sha256 is pinned in DECISIONS.md, because a second paid run
    would not give bit-identical vectors.
- `cache/tiktoken/`: the cl100k_base encoding file, checked against
  the sha256 pinned in the config before use.
- Query embeddings: no cache. The earlier plan for a query cache in
  `cache/embeddings/` (keyed by query hash) is superseded by
  DECISIONS.md, "Phase 5 semantic-v1 configuration": query embeddings
  are ephemeral. Consequence: two semantic eval runs may differ
  slightly, so the first complete run is the scored one.
- `cache/normalization/`: planned for Phase 6, keyed by case, track,
  model, prompt version.

## Out of scope before AfroTech
Hybrid retrieval, reranking, chunking (unless the eval shows long
articles are missed), no-match thresholds, ticket integration.
