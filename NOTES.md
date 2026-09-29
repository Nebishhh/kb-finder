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
  ~1,200 articles = ~2 hours first pass.
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

## Ideas
