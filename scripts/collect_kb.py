#!/usr/bin/env python
"""Phase 2A: collect the public Technology KB into local raw files.

Plain HTTP only. Walks the Technology category and its subcategories
(pre-order DFS, page order), dedupes articles by source ID, saves the
original response bytes per article, and writes a manifest incrementally.

Layout (all under data/raw/):
  articles/<id>.html          raw article bytes (gitignored)
  categories/<id>.html        raw category pages (gitignored)
  manifest.jsonl              one JSON record per article fetch attempt;
                              the last record per article_id wins
  category_manifest.jsonl     same idea for category pages

Run:  .venv/Scripts/python scripts/collect_kb.py [--limit N] [--refresh-categories]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

HOST = "td.usd.edu"
KB_PREFIX = "/TDClient/33/Portal/KB/"
START_URL = "https://td.usd.edu/TDClient/33/Portal/KB/Category/61/Technology"
ROBOTS_URL = "https://td.usd.edu/robots.txt"
USER_AGENT = ("KBFinder-Collector/0.1 "
              "(personal academic project; robots-respecting; 6s minimum delay)")
MIN_DELAY = 6.0
TIMEOUT = 30

# Phase 1 selectors (data/samples/inspection_report.md)
BODY_SELECTOR = "#ctl00_ctl00_cpContent_cpContent_divBody"
CATEGORY_LISTING = "#divCats"
ARTICLE_LINK_SELECTOR = "h3.gutter-bottom-xs > a[href]"
SUBCATEGORY_SELECTOR = "h3.category-title a[href]"

ARTICLE_ID_RE = re.compile(r"/KB/Article/(\d+)(?:/|$)")
CATEGORY_ID_RE = re.compile(r"/KB/Category/(\d+)(?:/|$)")
WS_RE = re.compile(r"\s+")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ pure helpers


def check_body(raw: bytes) -> tuple[bool, int, str | None]:
    """Lightweight validity check: the Phase 1 body selector must match exactly
    once and yield non-empty text. Not the parser. Returns (valid, length, error)."""
    try:
        markup = raw.decode("utf-8")
    except UnicodeDecodeError:
        markup = raw.decode("utf-8", errors="replace")
    matches = BeautifulSoup(markup, "html.parser").select(BODY_SELECTOR)
    if len(matches) != 1:
        return False, 0, f"body selector matched {len(matches)} elements (expected 1)"
    text = WS_RE.sub(" ", matches[0].get_text()).strip()
    if not text:
        return False, 0, "body selector matched but body text is empty"
    return True, len(text), None


def in_scope(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and p.netloc.lower() == HOST and p.path.startswith(KB_PREFIX)


def parse_category(raw: bytes, page_url: str) -> tuple[list[dict], list[dict]]:
    """Return (articles, subcategories) from the main listing only, in page order.
    Popular/Recent sidebars and breadcrumbs are outside #divCats and are ignored."""
    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
    main = soup.select_one(CATEGORY_LISTING)
    if main is None:
        return [], []
    articles, subcats = [], []
    for a in main.select(ARTICLE_LINK_SELECTOR):
        url = urldefrag(urljoin(page_url, a["href"]))[0]
        m = ARTICLE_ID_RE.search(url)
        if m and in_scope(url):
            articles.append({"id": m.group(1), "url": url})
    for a in main.select(SUBCATEGORY_SELECTOR):
        url = urldefrag(urljoin(page_url, a["href"]))[0]
        m = CATEGORY_ID_RE.search(url)
        if m and in_scope(url):
            subcats.append({"id": m.group(1), "url": url})
    return articles, subcats


def load_manifest(path: Path, key: str) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                records[rec[key]] = rec
    return records


def append_record(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def is_resumable(rec: dict | None, root: Path = ROOT) -> bool:
    """Skip only when: last fetch was HTTP 200, the saved file exists, and the
    body selector still extracts a non-empty body from that file."""
    if not rec or rec.get("http_status") != 200 or not rec.get("parse_valid"):
        return False
    if not rec.get("saved_path"):
        return False
    path = root / rec["saved_path"]
    if not path.is_file():
        return False
    return check_body(path.read_bytes())[0]


def save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ------------------------------------------------------------------ network


class Throttle:
    """One monotonic-clock throttle for every live request."""

    def __init__(self, delay: float, clock=time.monotonic, sleep=time.sleep):
        self.delay, self._clock, self._sleep = delay, clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.delay - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


class HttpFetcher:
    """requests-based fetcher: no cookies, no redirects, no retries."""

    def __init__(self):
        self.session = requests.Session()
        self.session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        self.session.headers.update({"User-Agent": USER_AGENT})

    def fetch(self, url: str) -> tuple[int | None, bytes, str | None]:
        try:
            r = self.session.get(url, timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            return None, b"", f"{type(exc).__name__}: {exc}"
        err = None if r.status_code == 200 else f"HTTP {r.status_code}"
        return r.status_code, r.content, err


class RobotsBlocked(Exception):
    pass


class Collector:
    def __init__(self, fetcher, root: Path = ROOT, limit: int | None = None,
                 refresh_categories: bool = False, throttle: Throttle | None = None,
                 log=print):
        self.fetcher = fetcher
        self.root = root
        self.raw = root / "data" / "raw"
        self.limit = limit
        self.refresh = refresh_categories
        self.throttle = throttle or Throttle(MIN_DELAY)
        self.log = log
        self.robots: robotparser.RobotFileParser | None = None
        self.article_manifest = self.raw / "manifest.jsonl"
        self.category_manifest = self.raw / "category_manifest.jsonl"
        self.articles = load_manifest(self.article_manifest, "article_id")
        self.stats = dict(discovered=0, fetched_ok=0, skipped=0, failed=0, invalid=0,
                          categories_walked=0, categories_cached=0, categories_failed=0,
                          subcategory_links=0, live_requests=0)
        self.seen_articles: set[str] = set()
        self.seen_categories: set[str] = set()
        self.processed = 0

    # -- robots
    def load_robots(self) -> None:
        self.throttle.wait()
        self.stats["live_requests"] += 1
        status, body, err = self.fetcher.fetch(ROBOTS_URL)
        rp = robotparser.RobotFileParser()
        if status == 200:
            rp.parse(body.decode("utf-8", errors="replace").splitlines())
        else:
            raise RobotsBlocked(f"could not fetch robots.txt ({err or status}); refusing to continue")
        self.robots = rp
        crawl = rp.crawl_delay(USER_AGENT)
        self.throttle.delay = max(MIN_DELAY, float(crawl) if crawl else 0.0)
        self.log(f"robots.txt ok; Crawl-delay={crawl}; using delay {self.throttle.delay}s")
        if not rp.can_fetch(USER_AGENT, START_URL):
            raise RobotsBlocked(f"robots.txt disallows the KB start path: {START_URL}")

    def allowed(self, url: str) -> bool:
        return self.robots.can_fetch(USER_AGENT, url)

    def live_get(self, url: str):
        self.throttle.wait()
        self.stats["live_requests"] += 1
        return self.fetcher.fetch(url)

    # -- categories
    def get_category(self, cat_id: str, url: str) -> bytes | None:
        path = self.raw / "categories" / f"{cat_id}.html"
        if path.is_file() and not self.refresh:
            self.stats["categories_cached"] += 1
            return path.read_bytes()
        if not self.allowed(url):
            self.log(f"  category {cat_id}: disallowed by robots.txt, skipped")
            self._cat_record(cat_id, url, None, None, "disallowed by robots.txt")
            self.stats["categories_failed"] += 1
            return None
        status, body, err = self.live_get(url)
        if status == 200:
            save_bytes(path, body)
            self._cat_record(cat_id, url, status, path, None)
            return body
        self._cat_record(cat_id, url, status, None, err)
        self.stats["categories_failed"] += 1
        self.log(f"  category {cat_id}: FAILED ({err})")
        return None

    def _cat_record(self, cat_id, url, status, path, error):
        append_record(self.category_manifest, {
            "category_id": cat_id, "url": url, "http_status": status,
            "saved_path": path.relative_to(self.root).as_posix() if path else None,
            "fetched_at": now_iso(), "error": error})

    # -- articles
    def process_article(self, art: dict, category_id: str) -> None:
        aid, url = art["id"], art["url"]
        self.processed += 1
        rec = self.articles.get(aid)
        if is_resumable(rec, self.root):
            self.stats["skipped"] += 1
            self.log(f"  article {aid}: skipped (valid)")
            return
        path = self.raw / "articles" / f"{aid}.html"
        rec = {"article_id": aid, "url": url, "http_status": None, "saved_path": None,
               "parse_valid": False, "body_length": 0, "fetched_at": now_iso(),
               "error": None, "category_id": category_id, "sha256": None}
        if not self.allowed(url):
            rec["error"] = "disallowed by robots.txt"
            self.stats["failed"] += 1
        else:
            status, body, err = self.live_get(url)
            rec["http_status"], rec["fetched_at"] = status, now_iso()
            if status == 200:
                save_bytes(path, body)
                rec["saved_path"] = path.relative_to(self.root).as_posix()
                rec["sha256"] = hashlib.sha256(body).hexdigest()
                valid, length, verr = check_body(body)
                rec.update(parse_valid=valid, body_length=length, error=verr)
                if valid:
                    self.stats["fetched_ok"] += 1
                else:
                    self.stats["invalid"] += 1
            else:
                rec["error"] = err
                self.stats["failed"] += 1
        self.articles[aid] = rec
        append_record(self.article_manifest, rec)
        state = "ok" if rec["parse_valid"] else f"FAILED ({rec['error']})"
        self.log(f"  article {aid}: {state}")

    def limit_reached(self) -> bool:
        return self.limit is not None and self.processed >= self.limit

    # -- walk
    def walk(self, cat_id: str, url: str) -> None:
        if cat_id in self.seen_categories or self.limit_reached():
            return
        self.seen_categories.add(cat_id)
        self.log(f"category {cat_id}")
        raw = self.get_category(cat_id, url)
        if raw is None:
            return
        self.stats["categories_walked"] += 1
        articles, subcats = parse_category(raw, url)
        self.stats["subcategory_links"] += len(subcats)
        for art in articles:
            if art["id"] in self.seen_articles:
                continue
            if self.limit_reached():
                return
            self.seen_articles.add(art["id"])
            self.stats["discovered"] += 1
            self.process_article(art, cat_id)
        for sub in subcats:
            if self.limit_reached():
                return
            self.walk(sub["id"], sub["url"])

    def run(self) -> None:
        self.load_robots()
        m = CATEGORY_ID_RE.search(START_URL)
        self.walk(m.group(1), START_URL)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N unique articles processed (fetched or skipped)")
    ap.add_argument("--refresh-categories", action="store_true",
                    help="re-fetch category pages instead of reusing saved ones")
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be >= 1")

    start = time.monotonic()
    c = Collector(HttpFetcher(), limit=args.limit, refresh_categories=args.refresh_categories)
    code = 0
    try:
        c.run()
    except RobotsBlocked as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("Interrupted; manifest is up to date for everything processed.", file=sys.stderr)
        code = 130
    s = c.stats
    print("\n== Summary ==")
    print(f"discovered unique articles : {s['discovered']}")
    print(f"successful fetches         : {s['fetched_ok']}")
    print(f"skipped valid/resumable    : {s['skipped']}")
    print(f"failed fetches             : {s['failed']}")
    print(f"invalid parses             : {s['invalid']}")
    print(f"categories walked/cached/failed: {s['categories_walked']}/"
          f"{s['categories_cached']}/{s['categories_failed']}")
    print(f"subcategory links seen     : {s['subcategory_links']}")
    print(f"live requests              : {s['live_requests']}")
    print(f"elapsed                    : {time.monotonic() - start:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
