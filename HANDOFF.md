# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## What was built (Phase 2: collect and parse)
- scripts/collect_kb.py: robots-respecting plain-HTTP collector. Pre-order DFS
  from Category/61 (Technology), dedupe by article ID, delay = max(6s, robots
  Crawl-delay), no cookies/redirects/retries, incremental JSONL manifest,
  resume rule (200 + saved file + body selector valid, re-checked from disk),
  category pages cached (`--refresh-categories` to refetch), `--limit N` counts
  articles processed in discovery order (skips count).
- scripts/parse_kb.py: deterministic parser -> data/parsed/articles.jsonl and
  parse_report.json. Own text walker (inline adds nothing, blocks break lines,
  zero-width chars dropped). Rules are in the module docstring.
- tests/test_parse_kb.py, tests/test_collect_kb.py: 35 offline unittest tests.
- .gitignore: data/raw/articles/, data/raw/categories/. AGENTS.md: 3 commands added.
- Layout: data/raw/{articles,categories}/ (ignored), data/raw/manifest.jsonl and
  category_manifest.jsonl (tracked), data/parsed/* (tracked).

## Results
- Tests: 35/35 pass. Live: `collect_kb.py --limit 5` fetched 5/5 valid
  (7 requests, 36s). Rerun: 5 skipped, 0 article fetches, category reused.
  Parser on those 5: 5/5 parsed, deterministic across runs.
- Fixtures: 13/13 parse; ITS STAFF ONLY found in exactly the 7 Phase 1 IDs,
  none elsewhere; fullText never contains staff text.

## Surprising
- Category 61 (root) lists articles directly AND has 12 subcategories, so the
  limited live run never descended into a subcategory (recursion is covered by
  offline tests only).
- Real Question/Answer articles exist (4571, 8120) plus a "Description / FAQs"
  template (4789) and "Online Resources for Students" (4822).
- `<footer>` in the body is NOT reliably boilerplate: 8721 has real content
  (Coyote Cash/eAccounts text and links) in it, 508 has a contact-with-info list.
  Per instruction footers are kept out of fullText and stored in `footer`;
  non-standard ones warn (footer_nonstandard). 8721 and 508 warn.
- 4911 has a standard footer; 233's staff box is mid-body, not last.

## What remains
- Human runs the full collection, then parse_kb.py, then reviews
  parse_report.json warnings (staff_only_*, footer_nonstandard, failures).
- Decide whether footer_nonstandard content (e.g. 8721) should count in retrieval.
- Human: SPEC.md updates (see chat proposals), DECISIONS.md entries, commit.
- Codex checkpoint review of collector + parser (WORKFLOW section 1).

## Unresolved human decisions
- Footer policy for non-standard footers (above).
- Whether staffOnly text/links are included in retrieval (measure later).

## Next intended step
Run full collection (~2h, resumable):
`.venv/Scripts/python scripts/collect_kb.py`
then `.venv/Scripts/python scripts/parse_kb.py`, review warnings, commit.
