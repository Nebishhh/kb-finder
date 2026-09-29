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
  ingestion, evaluation orchestration.
- Next.js, React, strict TypeScript: app and /api/search.
- PostgreSQL from the first retrieval slice. Local via the
  Supabase CLI; hosted on Supabase.
- pgvector: added only when semantic retrieval begins (Phase 5).
- No SQLite, no FTS5.

## Article model

Exact field shapes are defined in scripts/parse_kb.py.

```ts
type Article = {
  id: string;                // internal ID (assigned at ingestion)
  sourceArticleId: string;   // KB article ID
  title: string;
  url: string;
  category?: string;         // meta article:section
  tags: string[];
  description?: string;      // og:description
  publishedTime?: string;
  modifiedTime?: string;
  fullText: string;          // see fullText rules below
  question?: string;
  answer?: string;
  issue?: string;
  environment?: string;
  cause?: string;
  resolution?: string;
  sections: { heading: string; text: string }[]; // all sections, in order
  staffOnly?: object | null; // internal technician content, kept separate
  audienceLabels: string[];  // e.g. "USD STAFF ONLY"; content stays searchable
  footer?: string | null;    // original footer, kept for inspection
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

## Search API

`POST /api/search` is the only production retrieval path.
run_eval.py calls it. Ranking is never reimplemented elsewhere.

Request:

```ts
const SearchRequestSchema = z.object({
  query: z.string().min(1).max(5000),
  mode: z.literal("lexical"),
});
```

Only `lexical` exists until later experiments add modes.

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
  }[];
};
```

Planned modes, each added only when its experiment starts:
- `lexical`: Postgres full-text search ("Postgres FTS baseline",
  not BM25).
- `semantic`: text-embedding-3-small + pgvector, whole articles
  (template `article-v1`).
- `normalized_lexical`, `normalized_semantic`: query rewritten by
  the normalization model (prompt `normalize-v1`), then searched.

Mode gating (server-side):
- `DEFAULT_SEARCH_MODE` is always allowed.
- `EVAL_MODES_ENABLED=true` unlocks other modes for eval runs.
- Any other mode is rejected.
- Query embedding config must match stored embedding config, or
  the request fails loudly.

## Data handling
- Public KB article content may be stored.
- Issue text is ephemeral. Request bodies are never logged or
  stored.
- No names, IDs, emails, phone numbers, or ticket data.
- Semantic and normalized modes send query text to OpenAI.
  Issues are typed in manually with personal details removed.

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
- Positive cases: Top-1, Top-3, MRR, with raw counts
  (e.g. 14/20 (70%)). A hit means any ID in target_article_ids
  appears.
- Negative cases: top article, top score, ranked list, for
  qualitative review. No precision/recall until an abstention
  mechanism exists.

Caches (outside eval/, eval runs only):
- `cache/embeddings/`: keyed by query hash, embedding model,
  dimensions. Stores the hash, not the query text.
- `cache/normalization/`: keyed by case, track, model, prompt
  version.

## Out of scope before AfroTech
Hybrid retrieval, reranking, chunking (unless the eval shows long
articles are missed), no-match thresholds, ticket integration.
