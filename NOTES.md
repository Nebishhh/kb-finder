# NOTES

Human-owned. Agents read this file but never edit it.
Persistent notes for later phases and new ideas go here.

## Notes for later phases
- Phase 4: run_eval.py takes a required --cases <path> argument
  (no default). Agent smoke test uses
  tests/fixtures/fake_cases.json, which mirrors the SPEC.md
  schema example. Human runs:
  .venv/Scripts/python scripts/run_eval.py --mode lexical --cases eval/cases.json
- Claude Code 2.1.248: Write() deny rules are ignored; Edit() covers
  all file-editing tools. .claude/settings.json uses Edit only.
- Phase 2: td.usd.edu robots.txt sets Crawl-delay: 5. Collector must
  use a 6-second delay (robots.txt asks for 5; 6 for margin).
  ~1,200 articles = ~2 hours first pass (superseded, see corpus note below).
  Phase 1 used 2s for 28 requests before this was known.
- Phase 2: "ITS STAFF ONLY" sections (7/13 samples) are public, visible
  logged out. Content is a list of links to technician articles. Parser
  should store it as a separate field, not merged into fullText, so
  include/exclude can be measured. Collector follows category listings
  only, never links inside article bodies.
- Phase 2: no Question/Answer articles in the samples. Real templates:
  "Issue/Question / Environment / Cause / Resolution" (Cause sometimes
  missing) and "Question / Products or Services Involved /
  Instructions / Need More Help?". SPEC section fields may need
  adjusting when the parser is built.
- Phase 2: parser must not add spaces around inline tags (<a>, <b>,
  etc.), or text like "eAccounts, students" breaks.
- Category 12 lists 603 direct articles (WORKFLOW says 617); the rest
  are likely in subcategories, which the collector walks recursively.

- Corpus: 3,109 Technology articles (not ~1,200). First full run
  took ~5.3 hours at 6s. Parsed 3,109/3,109 after one retry (4902
  was a temporary server error page).
- Parser known limitations: 288, 289, 300, 2506 keep a
  staff_only_unmatched warning (documentation/prose mentions, not
  real staff blocks). 289's example panel is extracted to staffOnly
  ("Follow article XYZ"); harmless. 9050 has the whole article
  inside a <footer>, so its section fields are incomplete.
- ~92 footer_unclassified articles keep their leftover footer lines
  in fullText (substantive or article-specific). Not suppressed.
- 1,255 articles (40%) have staffOnly content. Whether staffOnly is
  searchable is a later decision, made with eval numbers.
- Some parser tests read data/raw/articles/ (gitignored) and skip
  when it is absent, e.g. on a fresh clone.

## Ideas


- Real sourceArticleId values are unpadded strings like "4778". The
  "00001" in SPEC's fake example is not real. Match IDs as exact
  strings in run_eval.py.
- Several student-facing articles (cases 13, 16, 18, 19 in the eval)
  link in staffOnly to technician articles whose IDs are not in the
  3,109 corpus. Indexing staffOnly text cannot surface an article
  that is absent, so keep that in mind when deciding whether
  staffOnly is searchable.
- Before hosting (Phase 7): add a request body size limit. request.json()
  reads the whole body before zod enforces the 5000 character limit.
- Low priority: rename vitest.config.ts to vitest.config.mts to remove
  the Vite configLoader warning. Do not set "type": "module" in
  package.json.
- Set $env:NEXT_TELEMETRY_DISABLED = "1" in the shell before any next
  command; .env.local is not read by the next dev parent process.