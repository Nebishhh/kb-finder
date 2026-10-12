# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## State
HEAD is efea436. Phase 7 (clean UI and hosting) is complete and
deployed. The app is live at https://kb-finder.nebanebish.com (Vercel
Hobby, iad1; Supabase free tier, us-east-1); the old
https://kb-finder-drab.vercel.app still works.
- **Production is unchanged:** the hosted app still serves semantic on
  article-v1 through `DEFAULT_SEARCH_MODE`, behind the 4-digit PIN gate
  with a global cap of 10 failed logins per UTC day (DECISIONS.md
  2026-10-04).
- **article-v2 experiment** (DECISIONS.md 2026-10-07 and 2026-10-12):
  staff-only text made searchable, never displayed, as a new versioned
  configuration beside v1. It is built and loaded on the LOCAL dev
  database only. The hosted database does NOT have the article-v2
  migration or embeddings.
  - `lexical-v2` and `semantic-v2` are eval-only: reachable only with
    `EVAL_MODES_ENABLED`, and neither can ever be
    `DEFAULT_SEARCH_MODE`.
  - Not scored. Eval v1 is ruled out (its results have been seen, and
    case_013 is labeled negative partly because its fix is
    staff-only). article-v2 is judged on eval v2, with the decision
    rule written before that run.
- Production retrieval stays frozen until after AfroTech (DECISIONS.md
  2026-10-05, D7 revised again). article-v2 is a local-only experiment
  and does not change the hosted app (DECISIONS.md 2026-10-07).
- DECISIONS.md is the source of truth for every number. Do not copy
  numbers from anywhere else.

## Commits since c14a469
- ba659ec docs: log the article-v2 experiment
- 0153549 feat: add article-v2 migration and lexical-v2 eval mode
- 43d6100 feat: add article-v2 embedding config and semantic-v2 eval mode
- 670b34d docs: record article-v2 built and loaded locally
- efea436 docs: record the article-v2 embedding cost

## What was built
- **article-v2, lexical (0153549):** migration
  `20261008211900_add_article_v2.sql` adds a generated column,
  `search_vector_v2` (the v1 vector plus staff text at weight D), and
  `kb_search_lexical_v2`. On the local dev database, v1 results for 7
  generic queries were identical before and after the migration (334
  rows: rank, id and exact score), and `search_vector_v2` differs from
  `search_vector` on exactly the 1,255 articles with staff text.
- **article-v2, semantic (43d6100):** config
  `config/embedding/article-v2.json` (v1 text, then
  `"\n\nStaff only:\n{text}"` when non-empty), `kb_article_v2_sha256`
  and `kb_search_semantic_v2`.
  - Config sha256
    `5cb0d08b0547a1ab611854c919d0f694eb3eb59314118775d265023c7fb12f33`.
  - Cache `cache/article_embeddings/5cb0d08b0547a1ab.jsonl`, 3,109
    rows, sha256
    `112a8f1275b35a306e75efea8c8674bbabada843cb24ef28119257ac356fa6c9`.
    Lives only on the owner's machine.
  - Input: 577,380 tokens before truncation, 569,605 sent, 1 article
    truncated (9431, as in v1). Projected cost $0.011392; the actual
    cost is not separately visible on the OpenAI usage page. One paid
    run; a later run had 3,109 cache hits and sent nothing.
  - The Python text builder matches `kb_article_v2_sha256` byte for
    byte on all 3,109 articles.
  - Loaded locally: both configs hold 3,109 rows, and
    `kb_search_semantic` and `kb_search_semantic_v2` each return 50
    results with no KBF error.
- **article-v1 untouched:** config, sha, cache file, text bytes and SQL
  checked unchanged before and after the refactor.
- **New test module:** `tests/test_article_v2_sql.py` (added to
  AGENTS.md commands).
- **Earlier work** (step 16 UI pass, custom domain, hardening
  migrations, gate, caps, TLS, `db_fingerprint.py`): see DECISIONS.md
  and the previous HANDOFF in git history (c14a469).

## Tests
- Not run in this session (docs-only change).
- Last recorded (DECISIONS.md 2026-10-12, after Part 2): Python 418
  (live DB included), unit 427, db 24, lint and build clean.
- No test calls OpenAI. Web tests block real fetch. The embedding
  tests block the network and never build a real client.

## Known limitations
- **Small eval set:** 21 positives, 4 negatives. One case moves a
  metric by about 5 points.
- **Track A margin:** see DECISIONS.md "2026-10-01: First semantic
  eval result (semantic-v1, frozen config)".
- **No abstention:** semantic always returns results; the UI says so.
- **Reruns can differ:** query embeddings are not cached.
- **Local-only caches:** both article embedding caches (v1 and v2) live
  only on the owner's machine, pinned by sha256 in DECISIONS.md.
- **Local and hosted differ:** local has the article-v2 migration and
  embeddings; hosted does not. Fingerprint diffs between them will show
  this.
- **KBF05 cost:** see DECISIONS.md "2026-10-01: Phase 5 semantic-v1
  configuration and decision rule (frozen)".
- **Staff-only links:** some student-facing articles link in staffOnly
  to technician articles not in the 3,109 corpus; indexing staff text
  cannot surface those (NOTES.md).
- **One shared PIN, global failed-login cap:** anyone can lock out new
  logins until the next UTC day by failing 10 times; devices already
  logged in keep working. Rotate the password or the cookie secret to
  revoke every cookie.
- **Global cap only:** one tester can use the whole day's 200
  searches. A search that fails after the cap check still counts.
- **Free-tier pause:** hosted Supabase pauses after 7 idle days (D9).
- **UI quirks (NOTES.md):** Samsung Internet and some in-app browsers
  force their own dark mode; demo in regular Chrome. Browsers cache
  favicons hard; check in an incognito tab. The UI copy hardcodes
  "3,109" in `app/search-client.tsx`, `app/page.tsx` and
  `app/gate/page.tsx`; update all three if the KB is re-collected.
- **Setup quirks:** `supabase start -x` still pulls some excluded
  images. There is no `seed.sql`. `next dev` needs
  `NEXT_TELEMETRY_DISABLED` set in the shell. Vite prints a
  configLoader warning.

## What remains
- **Eval v2 is the gate** for scoring article-v2, and for the
  comparison against the native KB search.
  - Queries are written blind; exclude any example queries logged in
    NOTES.md.
  - The decision rule is written into DECISIONS.md before the run.
  - The human runs it; agents never run a real eval.
- **Hosted rollout for article-v2, only if approved after eval v2:**
  1. Push the migration to hosted.
  2. Embed and load article-v2 on hosted in one transaction.
  3. Remap semantic to article-v2 in a later, separately logged
     release.
- **Follow-ups:**
  - Check the Supabase project is not paused before demos (D9).
  - Rotate the PIN after AfroTech (D2).
- **Parked:** hybrid retrieval or reranking, each as a separate, logged
  experiment. Phase 6, query normalization, is still planned later.

## Deploy rules
- Hosted env vars are Production only and set in Vercel.
- `RATE_LIMIT`, `ACCESS_GATE` and `EVAL_MODES_ENABLED` must never be
  set there.
- Keep "System Environment Variables" exposed: `VERCEL` drives the
  fail-closed rules.
- `DATABASE_URL` has no query string.
- Any env change needs a redeploy.
- No AI assistant open on screens that show secrets; secrets go only
  into the password manager and Vercel (DECISIONS.md step 15).

## Unresolved human decisions
- Whether article-v2 (staff-only text searchable) replaces v1: decided
  on eval v2 with a pre-written rule.
- When to version eval v2 (including the case_013 and case_025 labels).
- Whether to make the repo public. Git history contains the parsed
  corpus (`data/parsed`) and raw HTML samples (`data/samples/html`,
  `data/samples/category_html`).
