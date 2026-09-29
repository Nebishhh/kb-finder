# Decisions

## 2026-09-28: Phase 1 KB inspection

**Parse the normal article page, not the print page.**
The article body sits in one stable container on all 13 samples,
and Related Articles and the feedback widget are outside it. The
print page has the same body text but no meta tags, so category,
dates, and tags would be lost.

**Plain HTTP for collection, no browser automation.**
All 13 test sentences I copied from the browser were found in the
raw HTML, on both page versions. Category pages list every article
as a plain link with no pagination, so discovery is a recursive
walk of category pages.

**Strip tags without adding spaces for the server-rendered test.**
The first run failed one article because the script added a space
where it removed a link tag ("eAccounts , students"). That was a
bug in the test, not the site. Removing tags with no inserted space
passed 13/13.

**6-second delay between requests.**
robots.txt sets Crawl-delay: 5. I use 6 for margin. Phase 1 used
2 seconds for 28 requests before this was known.
