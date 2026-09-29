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
- Staff-only revision (parserVersion 2): top-level "Internal Staff Only" panels
  (also "Internal ITS Staff Only", "... - Internal Staff Only") go to staffOnly; an empty
  ITS box that is a direct body <div> is dropped; audience labels `^[A-Z]{2,10} STAFF ONLY$`
  (not ITS) go to new field `audienceLabels` (ordered, deduped, []), only the label line is
  removed, content stays in fullText/sections, never staffOnly. Footer rules unchanged.
- Footer rule (updated after the 8721 finding): full footer kept in `footer`
  (staff block removed first); only exact standard service-desk lines are
  removed from it; leftover lines go into fullText at the footer's position
  (not into sections/fields). Warnings: footer_nonstandard (informational),
  footer_unclassified (review; content still kept): leftover line mentions
  service/help desk or staff only, or footer is nested (not a direct body child).
- tests/test_parse_kb.py, tests/test_collect_kb.py: 46 offline unittest tests.
- .gitignore: data/raw/articles/, data/raw/categories/. AGENTS.md: 3 commands added.
- Layout: data/raw/{articles,categories}/ (ignored), data/raw/manifest.jsonl and
  category_manifest.jsonl (tracked), data/parsed/* (tracked).

## Results
- Tests: 63/63 pass (incl. 15 new; real-world ones read data/raw/articles and skip if absent).
- Full corpus: files 3109, parsed 3108, failed 1 (4902). staffOnly 1255 (was 1249),
  audienceLabels 13 articles (USD 9, HR 2, FINANCE 1, EPC 1).
- Warnings: staff_only_unmatched only 288, 289, 300, 2506; staff_only_unbounded none.
  footer_nonstandard 248 (unchanged); footer_unclassified 92 (was 93: 9050's only trigger
  was its FINANCE STAFF ONLY label line, now an audience label). Two parses byte-identical.
- 289 measured: its example "Internal ITS Staff Only" panel is a direct body child, so the
  structural rule now puts it in staffOnly (warning stays for prose mentions). 300's is
  nested and untouched.
- 9050: whole article sits in a <footer>, so environment/cause/resolution are null (known).
- 4902: TeamDynamix UnhandledError page served as HTTP 200 (no article). Refetch later;
  not a parser issue.

## What remains
- Human: SPEC.md audienceLabels addition (chat proposal), DECISIONS.md entries, commit.
- Refetch 4902 in a future collector run (resume rule retries it).
- Codex checkpoint review of collector + parser (WORKFLOW section 1).

## Unresolved human decisions
- Whether staffOnly text/links are included in retrieval (measure later).
- Whether 289's example panel in staffOnly is acceptable, and whether 288/289/300/2506 warrant a note.
