# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## State
Phase 5 (semantic retrieval) is complete.
- Both modes are built and evaluated.
- Semantic met the pre-registered decision rule, so it becomes the
  default when hosted. The switch has not been made yet:
  `DEFAULT_SEARCH_MODE` is unset (lexical), and `app/page.tsx:24`
  hard-codes `mode: "lexical"` in the UI's request.
- DECISIONS.md is the source of truth for every number. Do not copy
  numbers from anywhere else.

## Commits since ea38faf
- 717851e refactor: per-mode retriever map and shared search row
- 03d68c4 feat: pgvector embeddings schema and kb_search_semantic
- 5295a0e feat: offline article embedding pipeline
- 8772e3c feat: gated semantic search mode
- da1640d fix: address phase 5 review findings (Codex)
- dcbc846 docs: freeze phase 5 semantic configuration and decision rule
- ffb0c7c fix: never mix text and token inputs in one embedding request
- eec5d3c docs: record phase 5 article embed and pin cache hash
- 840226e docs: record first semantic eval result
- (this session, uncommitted) docs: SPEC, README, AGENTS and HANDOFF
  updated for Phase 5

## What was built
- **Migration** `20261001011735_add_semantic_embeddings.sql`.
  - pgvector, `embedding_configs`, and `article_embeddings` keyed on
    `source_article_id` (FK ON DELETE RESTRICT).
  - `kb_article_v1_sha256`, and `kb_search_semantic` with exact
    cosine search and KBF01-05 loud failures.
  - The lexical migration is unchanged.
- **`scripts/embed_kb.py`** with three commands:
  - `preflight`: zero API.
  - `embed --paid`: human only; cost ceiling, hard caps, journal.
  - `load`: no API, one transaction.
- **Config** `config/embedding/article-v1.json`. Both Python and the
  server derive the config sha256 from it.
- **Server:** `lib/embeddings/openai.ts` (key read at request time,
  fixed error codes, little-endian decoding), `lib/search/semantic.ts`
  and `lib/search/semantic-config.ts`. `/api/search` dispatches per
  mode, and semantic is gated by `EVAL_MODES_ENABLED` exactly "true".
- **`scripts/compare_eval_json.py`:** case-by-case report comparison.
  The lexical rerun at eec5d3c was identical to the baseline.
- **Credentials:** `supabase/config.toml` no longer passes
  `OPENAI_API_KEY` to Studio.

## Tests (as of ffb0c7c, the last code commit)
- Python: 332, run with `KB_FINDER_TEST_DATABASE_URL` set (live-DB
  tests included).
  - `tests.test_embed_kb`: 72.
  - `tests.test_semantic_sql`: 29.
  - `tests.test_compare_eval_json`: 14.
- Web unit tests (`npm run test:unit`): 90.
- `npm run test:db`: 8.
- No test calls OpenAI. Web tests block real fetch. The embedding
  tests block the network and never build a real client.

## Known limitations
- **Small set:** 21 positives, 4 negatives. One case moves a metric by
  about 5 points.
- **Thin margin:** Track A met the rule exactly (16/21 against 16).
  Track B Top-1 fell from 8 to 6.
- **Different fields:** the modes index different fields. Semantic
  embeds title and full text only.
- **Case movements:** case_021 is fixed by semantic. case_012 Track B
  regressed from rank 6 to 39.
- **No abstention:** semantic always returns up to 50 results.
  Negatives score 0.33 to 0.62, overlapping real matches.
- **Reruns can differ:** query embeddings are not cached, so semantic
  eval reruns can differ slightly. The first complete run is the
  scored one.
- **Local-only cache:** the article embedding cache lives only on the
  project owner's machine, pinned by sha256 in DECISIONS.md. Losing it
  means a new paid embed with different vectors and a new pin.
- **KBF05 cost:** the per-request staleness check costs about 47 ms.
  Revisit in Phase 7, per DECISIONS.md.
- **Exact model check:** both embedding clients require the response
  `model` to be exactly `text-embedding-3-small`.
- **staffOnly:** not searchable in either mode.
- **Setup quirks:** `supabase start -x` still pulls some excluded
  images. There is no `seed.sql`. `next dev` needs
  `NEXT_TELEMETRY_DISABLED` set in the shell. Vite prints a
  configLoader warning.

## What remains
- **Phase 7, clean UI and hosting:**
  - Make semantic the default: `DEFAULT_SEARCH_MODE` plus the UI's
    hard-coded mode.
  - The pre-hosting items in NOTES.md: the "Phase 7 (before hosting)"
    section, and the request body size limit under "Ideas".
  - WORKFLOW.md's Codex checkpoint 4 (keys, access gate, rate
    limiting, cost protection).
- **Phase 6, query normalization:** a stretch goal, only after Phase 7.
- **Separate, logged experiments:** hybrid retrieval, reranking, and a
  new, separately versioned eval set.

## Unresolved human decisions
- Whether staffOnly text is searchable (currently excluded in both
  modes).
- When to version a new eval set (case_013 and case_025 labels).
