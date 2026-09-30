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
- Staff-only handling (parserVersion 2): top-level "Internal Staff Only" panels go to
  staffOnly; "ITS STAFF ONLY" boxes go to staffOnly; department labels
  (`^[A-Z]{2,10} STAFF ONLY$`, not ITS) go to `audienceLabels` and content stays searchable.
- Layout: data/raw/{articles,categories}/ (ignored), data/raw/manifest.jsonl and
  category_manifest.jsonl (tracked), data/parsed/* (tracked).

## Codex checkpoint fixes (this session)
1. Standard service-desk line, now global. Before, only <footer> elements were cleaned, so
   213 articles kept "Please contact the Service Desk [if you need further assistance]" in
   fullText and sections. Now `drop_standard()` (exact `STANDARD_FOOTER_RE` match on a whole
   line, no fuzzy matching) runs on every body-derived line in `split_sections`, so fullText,
   sections and the canonical fields are clean wherever the line appears (footer, direct
   paragraph, panel, other section). Near-miss wording is kept. The `footer` field still holds
   the original footer text. Warnings did not move.
2. Category validation. `check_category()` requires `#divCats` to match exactly once (an
   empty category with the container is still valid; 184, 210, 216 are real examples).
   Applied to fresh and cached pages. Invalid fresh page: bytes saved for diagnosis, manifest
   `valid=false` + error, `categories_invalid` counted, not walked. Invalid cached page: not
   a cache hit, live refetch; if the refetch is valid the cache is repaired and it counts as
   `categories_recovered` (not fatal). Category manifest records gained `valid`,
   `article_count`, `subcategory_count`, `sha256`. Exit status: `Collector.exit_code()` is 1
   if any unresolved category failure/invalid category, article fetch failure, or invalid
   article parse remains; diagnostics are still written. Robots stop is 2, Ctrl-C is 130.
3. Atomic parse output. parse_kb.py writes articles.jsonl to a sibling `.tmp`, and replaces
   the real file only if every input parsed. On any failure the old file is kept, the temp is
   removed, exit 1. parse_report.json is always written (also on failure), via temp + replace.
- Fresh-clone coverage: tests/fixtures/regression_289_style_guide.html and
  regression_9050_footer_layout.html (minimal, labeled, placeholder text only).

## Results
- Tests: 89/89 pass (26 new). Parser tests that read data/raw/articles skip if it is absent.
- Full corpus ultimately collected and parses 3109/3109 (files 3109, parsed 3109, failed 0).
  Article 4902 was a TeamDynamix error page served as HTTP 200; it succeeded on retry.
- Exact standard line occurrences after the fix: 0 in fullText, sections, question, answer,
  issue, environment, cause, resolution (was 213 articles).
- Warnings unchanged: footer_nonstandard 248, footer_unclassified 92, staff_only_unmatched 4
  (288, 289, 300, 2506). Two parser runs byte-identical.
- data/parsed/articles.jsonl changed only for the 213 affected articles.

## Known limitations
- 9050 has the whole article inside a <footer>: environment/cause/resolution are null (text
  is in fullText).
- 288, 289, 300, 2506 keep staff_only_unmatched (documentation/prose mentions). 289's example
  panel goes to staffOnly ("Follow article XYZ"); harmless.
- ~92 footer_unclassified articles keep leftover footer lines in fullText (not suppressed).
- Old category manifest records (before this change) have no `valid` field; only the saved
  file itself is re-validated.
- Collector fixes are covered offline only; the live collector was not run this session.

## What remains
- Human: DECISIONS.md entries, commit.

## Unresolved human decisions
- Whether staffOnly text/links are included in retrieval (measure later).
- Whether 289's example panel in staffOnly is acceptable, and whether 288/289/300/2506 warrant a note.
