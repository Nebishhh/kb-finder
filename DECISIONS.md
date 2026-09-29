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

## 2026-09-29: Phase 2 collection and parsing

**Collect all of Technology, not a subset.**
The recursive walk found 3,109 unique articles, about 2.5x the
estimate. All are indexed so search is tested against the real
candidate pool.

**Raw HTML stays local; parsed output and manifests are committed.**
Raw HTML is ~55 MB and can be regenerated. The parsed articles
(9.4 MB) and manifests are what later phases build on.

**Staff-only content is kept separate from searchable text.**
40% of articles have an "ITS STAFF ONLY" box or "Internal Staff
Only" panel. It goes into its own field so I can measure later
whether including it helps or hurts search.

**Department labels are not staff-only.**
"USD/HR/FINANCE/EPC STAFF ONLY" boxes describe who the content is
for, and some hold the whole answer (e.g. 10177's Resolution). I
record the label and keep the content searchable.

**Footers: remove only the standard contact line.**
I first assumed footers were boilerplate. Article 8721 showed real
content in its footer, so now only the standard service-desk line
is removed and everything else stays searchable.

**Flag instead of guess.**
When the parser can't identify a layout with a clear rule, it keeps
the text and raises a warning. The remaining warnings were reviewed
and accepted as known limitations.