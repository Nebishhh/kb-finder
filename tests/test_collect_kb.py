"""Offline tests for scripts/collect_kb.py. No sockets: fake fetcher only."""
import collections
import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import collect_kb  # noqa: E402

CAT = ROOT / "data" / "samples" / "category_html"
ART = ROOT / "data" / "samples" / "html"
BASE = "https://td.usd.edu/TDClient/33/Portal/KB/"
ROBOTS = b"User-agent: *\nCrawl-delay: 5\nDisallow: /admin\n"


def cat_page(articles, subcats):
    """SYNTHETIC category page using the Phase 1 listing structure."""
    a = "".join('<h3 class="gutter-bottom-xs"><a href="/TDClient/33/Portal/KB/Article/%s/T%s">T</a></h3>'
                % (i, i) for i in articles)
    s = "".join('<h3 class="category-title"><a href="/TDClient/33/Portal/KB/Category/%s/C%s">C</a></h3>'
                % (i, i) for i in subcats)
    side = ('<div id="ctl00_ctl00_cpContent_cpContent_divPopular">'
            '<a href="/TDClient/33/Portal/KB/Article/999/Pop">P</a></div>'
            '<a href="/TDClient/33/Portal/KB/Category/61/Technology">crumb</a>')
    return ('<html><body>%s<div id="divCats">%s%s</div></body></html>' % (side, s, a)).encode()


def article_page(aid, valid=True):
    body = "<p>Body %s</p>" % aid if valid else ""
    return ('<html><body><div id="ctl00_ctl00_cpContent_cpContent_divBody">' + body +
            "</div></body></html>").encode()


class FakeFetcher:
    def __init__(self, pages, robots=ROBOTS):
        self.pages = {BASE + k: v for k, v in pages.items()}
        self.pages["https://td.usd.edu/robots.txt"] = (200, robots)
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        status, body = self.pages.get(url, (404, b""))
        return status, body, None if status == 200 else "HTTP %d" % status


class FakeClockThrottle(collect_kb.Throttle):
    def __init__(self):
        self.now = 0.0
        self.slept = []
        super().__init__(6.0, clock=lambda: self.now, sleep=self._sleep)

    def _sleep(self, s):
        self.slept.append(s)
        self.now += s


def run(tmp, pages, robots=ROBOTS, **kw):
    f = FakeFetcher(pages, robots)
    c = collect_kb.Collector(f, root=Path(tmp), throttle=FakeClockThrottle(),
                             log=lambda *_: None, **kw)
    try:
        c.run()
    except collect_kb.RobotsBlocked:
        pass
    return c, f


TREE = {
    "Category/61/Technology": (200, cat_page([1], [10, 20])),
    "Category/10/C10": (200, cat_page([2, 1], [11])),
    "Category/11/C11": (200, cat_page([3], [61])),   # loops back to the root
    "Category/20/C20": (200, cat_page([4], [])),
    **{"Article/%d/T%d" % (i, i): (200, article_page(i)) for i in (1, 2, 3, 4)},
}


class DiscoveryTests(unittest.TestCase):
    @unittest.skipUnless(CAT.exists(), "sample HTML data/samples/category_html not in repo (gitignored)")
    def test_real_fixtures(self):
        arts, subs = collect_kb.parse_category((CAT / "12.html").read_bytes(), BASE + "Category/12/x")
        self.assertEqual((len(arts), len({a["id"] for a in arts}), len(subs)), (603, 603, 18))
        arts, subs = collect_kb.parse_category((CAT / "23.html").read_bytes(), BASE + "Category/23/x")
        self.assertEqual((len(arts), len(subs)), (109, 0))

    def test_sidebar_and_breadcrumb_ignored(self):
        arts, subs = collect_kb.parse_category(cat_page([1], [2]), BASE + "Category/5/x")
        self.assertEqual([a["id"] for a in arts], ["1"])
        self.assertEqual([s["id"] for s in subs], ["2"])

    def test_scope(self):
        self.assertTrue(collect_kb.in_scope(BASE + "Article/1/x"))
        self.assertFalse(collect_kb.in_scope("https://example.com/TDClient/33/Portal/KB/Article/1/x"))
        self.assertFalse(collect_kb.in_scope("https://td.usd.edu/other"))


class WalkTests(unittest.TestCase):
    def test_recursive_walk_dedupe_and_loop(self):
        with tempfile.TemporaryDirectory() as t:
            c, f = run(t, TREE)
            self.assertEqual(c.stats["discovered"], 4)  # article 1 is listed twice
            self.assertEqual(c.stats["fetched_ok"], 4)
            self.assertEqual(sum(u.endswith("/Article/1/T1") for u in f.calls), 1)
            self.assertEqual(sum("Category/61" in u for u in f.calls), 1)
            m = collect_kb.load_manifest(Path(t) / "data/raw/manifest.jsonl", "article_id")
            self.assertEqual(sorted(m), ["1", "2", "3", "4"])
            for k in ("article_id", "url", "http_status", "saved_path", "parse_valid",
                      "body_length", "fetched_at", "error"):
                self.assertIn(k, m["2"])
            self.assertEqual(m["2"]["category_id"], "10")
            self.assertTrue((Path(t) / m["2"]["saved_path"]).is_file())

    def test_raw_bytes_saved_verbatim(self):
        with tempfile.TemporaryDirectory() as t:
            run(t, TREE)
            self.assertEqual((Path(t) / "data/raw/articles/3.html").read_bytes(), article_page(3))

    def test_limit_counts_skips_and_resume(self):
        with tempfile.TemporaryDirectory() as t:
            c1, f1 = run(t, TREE, limit=2)
            self.assertEqual(c1.stats["fetched_ok"], 2)
            self.assertEqual(sum("/Article/" in u for u in f1.calls), 2)
            c2, f2 = run(t, TREE, limit=2)
            self.assertEqual((c2.stats["skipped"], c2.stats["fetched_ok"]), (2, 0))
            self.assertFalse([u for u in f2.calls if "/Article/" in u])
            self.assertFalse([u for u in f2.calls if "/Category/" in u])  # cached categories
            c3, f3 = run(t, TREE, limit=2, refresh_categories=True)
            self.assertTrue([u for u in f3.calls if "/Category/" in u])
            self.assertEqual(c3.stats["fetched_ok"], 0)

    def test_limit_bounds_discovery(self):
        with tempfile.TemporaryDirectory() as t:
            c, f = run(t, TREE, limit=1)
            self.assertFalse([u for u in f.calls if "Category/10" in u or "Category/20" in u])

    def test_resume_rule(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            p = root / "data/raw/articles/9.html"
            p.parent.mkdir(parents=True)
            p.write_bytes(article_page(9))
            good = {"http_status": 200, "saved_path": "data/raw/articles/9.html", "parse_valid": True}
            self.assertTrue(collect_kb.is_resumable(good, root))
            self.assertFalse(collect_kb.is_resumable({**good, "http_status": 500}, root))
            self.assertFalse(collect_kb.is_resumable({**good, "parse_valid": False}, root))
            self.assertFalse(collect_kb.is_resumable({**good, "saved_path": "data/raw/articles/8.html"}, root))
            self.assertFalse(collect_kb.is_resumable(None, root))
            p.write_bytes(article_page(9, valid=False))  # file exists but body is now empty
            self.assertFalse(collect_kb.is_resumable(good, root))

    def test_failures_recorded_and_retried_later(self):
        pages = dict(TREE)
        pages["Article/2/T2"] = (500, b"")
        pages["Article/3/T3"] = (200, article_page(3, valid=False))
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, pages)
            self.assertEqual((c.stats["failed"], c.stats["invalid"], c.stats["fetched_ok"]), (1, 1, 2))
            m = collect_kb.load_manifest(Path(t) / "data/raw/manifest.jsonl", "article_id")
            self.assertIsNone(m["2"]["saved_path"])
            self.assertFalse(m["3"]["parse_valid"])
            c2, f2 = run(t, TREE)  # site recovered
            self.assertEqual((c2.stats["skipped"], c2.stats["fetched_ok"]), (2, 2))
            self.assertEqual(sum("/Article/" in u for u in f2.calls), 2)

    def test_robots_disallow_stops(self):
        with tempfile.TemporaryDirectory() as t:
            c, f = run(t, TREE, robots=b"User-agent: *\nDisallow: /TDClient/\n")
            self.assertEqual(f.calls, ["https://td.usd.edu/robots.txt"])
            self.assertEqual(c.stats["discovered"], 0)

    def test_delay_is_max_of_6_and_crawl_delay(self):
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, TREE)
            self.assertEqual(c.throttle.delay, 6.0)
            self.assertTrue(all(s >= 5.999 for s in c.throttle.slept))
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, TREE, robots=b"User-agent: *\nCrawl-delay: 10\n")
            self.assertEqual(c.throttle.delay, 10.0)

    def test_user_agent_has_no_personal_info(self):
        self.assertEqual(collect_kb.USER_AGENT, "KBFinder-Collector/0.1 "
                         "(personal academic project; robots-respecting; 6s minimum delay)")

    @unittest.skipUnless(ART.exists(), "sample HTML data/samples/html not in repo (gitignored)")
    def test_fixture_bodies_are_valid(self):
        for p in sorted(ART.glob("*[0-9].html")):
            if p.stem.endswith("_print"):
                continue
            valid, n, err = collect_kb.check_body(p.read_bytes())
            self.assertTrue(valid and n > 0, (p.name, err))


BAD_CAT = b"<html><body><h1>UnhandledError</h1><p>Something went wrong</p></body></html>"


def cat_manifest(t):
    return collect_kb.load_manifest(Path(t) / "data/raw/category_manifest.jsonl", "category_id")


class CategoryValidityTests(unittest.TestCase):
    def test_check_category(self):
        self.assertEqual(collect_kb.check_category(cat_page([1, 2], [3])), (True, 2, 1, None))
        self.assertEqual(collect_kb.check_category(cat_page([], []))[:3], (True, 0, 0))  # real empty category
        valid, na, ns, err = collect_kb.check_category(BAD_CAT)
        self.assertFalse(valid)
        self.assertIn("matched 0", err)
        two = b'<div id="divCats"></div><div id="divCats"></div>'
        self.assertFalse(collect_kb.check_category(two)[0])
        for p in sorted(CAT.glob("*.html")):
            self.assertTrue(collect_kb.check_category(p.read_bytes())[0], p.name)

    def test_http_200_malformed_category_is_invalid(self):
        pages = {**TREE, "Category/20/C20": (200, BAD_CAT)}
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, pages)
            self.assertEqual((c.stats["categories_invalid"], c.stats["categories_failed"]), (1, 0))
            rec = cat_manifest(t)["20"]
            self.assertFalse(rec["valid"])
            self.assertEqual(rec["http_status"], 200)
            self.assertIn("matched 0", rec["error"])
            self.assertEqual(rec["sha256"], hashlib.sha256(BAD_CAT).hexdigest())
            self.assertEqual(c.exit_code(), 1)

    def test_valid_category_manifest_fields(self):
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, TREE)
            rec = cat_manifest(t)["10"]
            self.assertEqual((rec["valid"], rec["article_count"], rec["subcategory_count"], rec["error"]),
                             (True, 2, 1, None))
            self.assertEqual(rec["sha256"], hashlib.sha256(TREE["Category/10/C10"][1]).hexdigest())

    def test_invalid_category_cannot_silently_truncate(self):
        pages = {**TREE, "Category/10/C10": (200, BAD_CAT)}
        with tempfile.TemporaryDirectory() as t:
            c, f = run(t, pages)
            # C10's articles/subcategory (2, 3 via C11) are not fetched, and that is reported...
            self.assertFalse([u for u in f.calls if "/Article/2/" in u or "/Article/3/" in u])
            self.assertEqual(c.stats["categories_invalid"], 1)
            self.assertEqual(c.exit_code(), 1)
            # ...while siblings are still walked.
            self.assertEqual(c.stats["fetched_ok"], 2)  # articles 1 and 4

    def test_invalid_page_not_a_cache_hit(self):
        pages = {**TREE, "Category/20/C20": (200, BAD_CAT)}
        with tempfile.TemporaryDirectory() as t:
            run(t, pages)
            c2, f2 = run(t, pages)  # still broken on the server
            self.assertTrue([u for u in f2.calls if "Category/20" in u])  # refetched, not cached
            self.assertEqual((c2.stats["categories_invalid"], c2.stats["categories_recovered"]), (1, 0))
            self.assertEqual(c2.exit_code(), 1)

    def test_bad_cache_repaired_by_live_refetch_exits_zero(self):
        with tempfile.TemporaryDirectory() as t:
            run(t, TREE)
            cached = Path(t) / "data/raw/categories/20.html"
            cached.write_bytes(BAD_CAT)  # corrupt a cache entry
            c, f = run(t, TREE)
            self.assertEqual(sum("Category/20" in u for u in f.calls), 1)
            self.assertEqual(c.stats["categories_recovered"], 1)
            self.assertEqual(c.stats["categories_invalid"], 0)
            self.assertEqual(cached.read_bytes(), TREE["Category/20/C20"][1])  # repaired
            self.assertTrue(cat_manifest(t)["20"]["valid"])
            self.assertEqual(c.stats["skipped"], 4)  # traversal succeeded, articles resumable
            self.assertEqual(c.exit_code(), 0)

    def test_bad_cache_and_bad_refetch_is_unresolved(self):
        pages = {**TREE, "Category/20/C20": (200, BAD_CAT)}
        with tempfile.TemporaryDirectory() as t:
            run(t, TREE)
            (Path(t) / "data/raw/categories/20.html").write_bytes(BAD_CAT)
            c, _ = run(t, pages)
            self.assertEqual((c.stats["categories_invalid"], c.stats["categories_recovered"]), (1, 0))
            self.assertEqual(c.exit_code(), 1)

    def test_bad_cache_refetch_http_failure_is_unresolved(self):
        pages = {**TREE, "Category/20/C20": (500, b"")}
        with tempfile.TemporaryDirectory() as t:
            run(t, TREE)
            (Path(t) / "data/raw/categories/20.html").write_bytes(BAD_CAT)
            c, _ = run(t, pages)
            self.assertEqual(c.stats["categories_failed"], 1)
            self.assertEqual(c.exit_code(), 1)


class ExitStatusTests(unittest.TestCase):
    def test_success_is_zero(self):
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, TREE)
            self.assertEqual(c.exit_code(), 0)

    def test_category_http_failure_is_nonzero(self):
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, {**TREE, "Category/20/C20": (500, b"")})
            self.assertEqual(c.stats["categories_failed"], 1)
            self.assertEqual(c.exit_code(), 1)

    def test_article_http_failure_is_nonzero(self):
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, {**TREE, "Article/2/T2": (500, b"")})
            self.assertEqual(c.exit_code(), 1)

    def test_invalid_article_parse_is_nonzero(self):
        with tempfile.TemporaryDirectory() as t:
            c, _ = run(t, {**TREE, "Article/3/T3": (200, article_page(3, valid=False))})
            self.assertEqual(c.exit_code(), 1)

    def test_main_returns_collector_exit_code(self):
        class C:
            stats = collections.defaultdict(int)
            def __init__(self, *a, **k): pass
            def run(self): pass
            def exit_code(self): return 1
        orig = collect_kb.Collector
        collect_kb.Collector = C
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(collect_kb.main([]), 1)
        finally:
            collect_kb.Collector = orig


if __name__ == "__main__":
    unittest.main()
