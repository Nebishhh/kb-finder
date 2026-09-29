# HANDOFF

Disposable. Rewritten by the agent at the end of every session.
Anything that must survive across sessions goes in NOTES.md.

## What was built (Phase 1: KB inspection)
- scripts/inspect_kb.py: fetches robots.txt once, then the 13 sample
  articles, their PrintArticle pages, and the 2 category pages (2s
  delay, no retries, redirects not followed). Saves raw bytes and writes
  data/samples/inspection_report.md/.json. `--offline` re-analyzes
  saved HTML with sockets blocked (zero network requests).
- requirements.txt: requests, beautifulsoup4 (pinned).
- Fixtures: data/samples/html/ (26 files), data/samples/category_html/
  (12.html, 23.html). All 28 requests returned 200; robots.txt allows
  /TDClient/.
- The network fetch ran exactly once (2026-09-29T02:22Z UTC). All later
  analysis runs were --offline.

## Results
- Server-rendered gate: **13/13 pass.** The first analysis failed 8721
  because the tag stripper inserted a space at each tag boundary
  ("<a>eAccounts</a>," became "eAccounts ,"). That was an
  implementation bug. The fixed gate removes markup without inserting
  anything; all other gate rules are unchanged.
- Category discovery works with plain HTTP. Each page lists all of its
  direct articles on one page, and the count matches the "Articles (N)"
  header (603 for cat 12, 109 for cat 23). There is no pagination.
  Subcategories are plain links (18 under cat 12, none under 23) and
  must be walked recursively. Postbacks are used only for
  subscribe/search.
- Recommended body selector: #ctl00_ctl00_cpContent_cpContent_divBody
  on the normal page. It matches exactly once in all 13 samples, and
  Related Articles (#ctl00_ctl00_cpContent_cpContent_divKB, sidebar)
  and feedback are outside it. Print #divBody gives identical body
  text but the print page has no meta tags, so the normal page wins.

## Surprising
- **robots.txt has `Crawl-delay: 5`.** Phase 1 used 2s for its 28 page
  requests before this was known. Future collection will honor the 5s
  Crawl-delay (human decision; the human updates the project docs).
- No sample has a Question/Answer pair. The templates are
  "Issue/Question / Environment / [Cause] / Resolution" and
  "Question / Products or Services Involved / Instructions / Need More
  Help?". The latter has no Answer heading.
- "ITS STAFF ONLY" sections appear inside the body container in 7/13
  articles. They are publicly visible (human-checked logged out on 233)
  and consist of links to technician articles. Not decided in Phase 1.
- Body text from BeautifulSoup get_text(" ") puts a space before
  punctuation after inline tags ("eAccounts , students"), so the
  phrase-in-body check still fails for 8721. This affects Phase 2 body
  extraction, not the gate.
- Cat 12 lists 603 direct articles, but WORKFLOW.md says 617. The
  difference is unexplained (it may count subcategory articles).
- .gitattributes had no -text rules for the fixture directories when
  this session checked it, and git log showed no new commit.
- 5/13 articles have no tags.

## What remains (Phase 2 parsing decisions)
- ITS STAFF ONLY: likely store it as a separate field rather than merge
  it into body text, so including or excluding it can be measured later.
- Body text extraction: do not insert spaces at inline element
  boundaries (a, strong, em, span).
- Section mapping: treat "Issue/Question" as the Issue section and
  allow a missing Cause. Decide how to map the "Question / Products or
  Services Involved / Instructions / Need More Help?" template.
- Human: confirm .gitattributes, then review and commit the Phase 1
  files.

## Next intended step
Phase 2 collector (scripts/collect_kb.py): plain HTTP recursive
category walk, dedupe by article ID, 5s delay honoring Crawl-delay,
validity check with the recommended body selector.
