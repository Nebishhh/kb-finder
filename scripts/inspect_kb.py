"""Phase 1 KB inspection.

Fetches the supplied sample article and category pages (plus the
PrintArticle version of each article) once, saves raw bytes, and writes
an inspection report.

    .venv/Scripts/python scripts/inspect_kb.py            # fetch, then analyze
    .venv/Scripts/python scripts/inspect_kb.py --offline  # analyze saved HTML only
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import unicodedata
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"
URLS_FILE = SAMPLES / "urls.txt"
CATEGORY_URLS_FILE = SAMPLES / "category_urls.txt"
HTML_DIR = SAMPLES / "html"
CATEGORY_HTML_DIR = SAMPLES / "category_html"
REPORT_MD = SAMPLES / "inspection_report.md"
REPORT_JSON = SAMPLES / "inspection_report.json"

USER_AGENT = "KBFinder-Inspection/0.1 (personal academic project; robots-respecting; 2s delay)"
DELAY_SECONDS = 2.0
TIMEOUT_SECONDS = 30
KB_ROOT_PATH = "/TDClient/33/Portal/KB/"
PRINT_PATH = "/TDClient/33/Portal/KB/PrintArticle?ID={id}"

ARTICLE_ID_RE = re.compile(r"/KB/Article/(\d+)(?:/|$)")
CATEGORY_ID_RE = re.compile(r"/KB/Category/(\d+)(?:/|$)")
ARTICLE_HREF_RE = re.compile(r"/KB/Article/(\d+)")

# Candidate body selectors under test (found by reading the saved samples).
NORMAL_BODY_SELECTOR = "#ctl00_ctl00_cpContent_cpContent_divBody"
PRINT_BODY_SELECTOR = "#divBody"  # first child div.gutter-bottom is the "Body" label
NORMAL_RELATED_SELECTOR = "#ctl00_ctl00_cpContent_cpContent_divKB"
PRINT_RELATED_SELECTOR = "#divRelatedArticles"
NORMAL_FEEDBACK_SELECTOR = "#ctl00_ctl00_cpContent_cpContent_divFeedback2"
NORMAL_TAGS_SELECTOR = "#ctl00_ctl00_cpContent_cpContent_divTags"
META_PROPERTIES = ["article:section", "article:published_time",
                   "article:modified_time", "og:description", "og:title", "og:url"]


# ---------------------------------------------------------------- inputs


def read_article_inputs() -> list[dict]:
    items = []
    for line_no, raw in enumerate(URLS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        url, sep, phrase = raw.partition(" | ")
        url = url.strip()
        match = ARTICLE_ID_RE.search(url)
        items.append({
            "line": line_no,
            "url": url,
            "phrase": phrase if sep else None,
            "article_id": match.group(1) if match else None,
        })
    return items


def read_category_inputs() -> list[dict]:
    items = []
    for raw in CATEGORY_URLS_FILE.read_text(encoding="utf-8").splitlines():
        url = raw.strip()
        if not url:
            continue
        match = CATEGORY_ID_RE.search(url)
        items.append({"url": url, "category_id": match.group(1) if match else None})
    return items


def build_targets(articles: list[dict], categories: list[dict]) -> list[dict]:
    """Every URL this run may fetch: supplied URLs plus one print URL per article."""
    targets = []
    for a in articles:
        if not a["article_id"]:
            continue
        parts = urlsplit(a["url"])
        origin = f"{parts.scheme}://{parts.netloc}"
        targets.append({"kind": "article", "id": a["article_id"], "url": a["url"],
                        "path": HTML_DIR / f"{a['article_id']}.html"})
        targets.append({"kind": "print", "id": a["article_id"],
                        "url": origin + PRINT_PATH.format(id=a["article_id"]),
                        "path": HTML_DIR / f"{a['article_id']}_print.html"})
    for c in categories:
        if not c["category_id"]:
            continue
        targets.append({"kind": "category", "id": c["category_id"], "url": c["url"],
                        "path": CATEGORY_HTML_DIR / f"{c['category_id']}.html"})
    return targets


# ---------------------------------------------------------------- fetching


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    adapter = HTTPAdapter(max_retries=0)  # no retries
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_all(targets: list[dict]) -> dict:
    """Fetch robots.txt once, then each allowed target once, 2s apart."""
    origins = {f"{urlsplit(t['url']).scheme}://{urlsplit(t['url']).netloc}" for t in targets}
    if len(origins) != 1:
        raise SystemExit(f"Expected a single origin, got: {sorted(origins)}")
    origin = origins.pop()
    robots_url = origin + "/robots.txt"
    session = make_session()

    robots = {"url": robots_url, "fetched_at": utc_now(), "status": None, "error": None,
              "text": None, "interpretation": None, "kb_root_allowed": None}
    parser = urllib.robotparser.RobotFileParser(robots_url)
    try:
        resp = session.get(robots_url, timeout=TIMEOUT_SECONDS, allow_redirects=False)
        robots["status"] = resp.status_code
        if resp.status_code == 200:
            robots["text"] = resp.text
            parser.parse(resp.text.splitlines())
            robots["interpretation"] = "parsed"
        elif resp.status_code in (401, 403):
            parser.disallow_all = True
            robots["interpretation"] = "access denied: treated as disallow all (robotparser convention)"
        elif 400 <= resp.status_code < 500:
            parser.allow_all = True
            robots["interpretation"] = "not found: treated as allow all (robotparser convention)"
        else:
            robots["interpretation"] = "unusable response: stopping"
    except requests.RequestException as exc:
        robots["error"] = f"{type(exc).__name__}: {exc}"
        robots["interpretation"] = "fetch failed: stopping"

    fetch = {"mode": "network", "user_agent": USER_AGENT, "delay_seconds": DELAY_SECONDS,
             "started_at": robots["fetched_at"], "robots": robots, "stopped": None,
             "requests": []}

    if robots["interpretation"] in ("unusable response: stopping", "fetch failed: stopping"):
        fetch["stopped"] = "robots.txt could not be fetched; no pages requested"
        return fetch

    robots["kb_root_allowed"] = parser.can_fetch(USER_AGENT, origin + KB_ROOT_PATH)
    if not robots["kb_root_allowed"]:
        fetch["stopped"] = f"robots.txt disallows {KB_ROOT_PATH}; no pages requested"
        return fetch

    for t in targets:
        entry = {"kind": t["kind"], "id": t["id"], "url": t["url"],
                 "saved_path": None, "status": None, "content_type": None,
                 "bytes": None, "location": None, "fetched_at": None,
                 "robots_allowed": parser.can_fetch(USER_AGENT, t["url"]), "error": None}
        fetch["requests"].append(entry)
        if not entry["robots_allowed"]:
            entry["error"] = "skipped: disallowed by robots.txt"
            print(f"SKIP (robots) {t['url']}")
            continue
        time.sleep(DELAY_SECONDS)
        entry["fetched_at"] = utc_now()
        try:
            # Redirects are not followed: that would be fetching a URL we were not given.
            resp = session.get(t["url"], timeout=TIMEOUT_SECONDS, allow_redirects=False)
        except requests.RequestException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            print(f"FAIL {t['url']}: {entry['error']}")
            continue
        entry["status"] = resp.status_code
        entry["content_type"] = resp.headers.get("Content-Type")
        entry["bytes"] = len(resp.content)
        entry["location"] = resp.headers.get("Location")
        if resp.status_code == 200:
            t["path"].parent.mkdir(parents=True, exist_ok=True)
            t["path"].write_bytes(resp.content)
            entry["saved_path"] = t["path"].relative_to(ROOT).as_posix()
            print(f"OK   {resp.status_code} {len(resp.content):>7} B  {t['url']}")
        else:
            entry["error"] = f"HTTP {resp.status_code}"
            print(f"FAIL {resp.status_code} {t['url']}")
    return fetch


def load_saved_fetch_log(targets: list[dict]) -> dict:
    """Offline: reuse the fetch log from the last network run; never touch the network."""
    if REPORT_JSON.exists():
        previous = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
        fetch = previous.get("fetch")
        if fetch:
            return fetch
    # No previous log: reconstruct from files on disk.
    requests_log = []
    for t in targets:
        exists = t["path"].exists()
        requests_log.append({
            "kind": t["kind"], "id": t["id"], "url": t["url"],
            "saved_path": t["path"].relative_to(ROOT).as_posix() if exists else None,
            "status": None, "content_type": None,
            "bytes": t["path"].stat().st_size if exists else None,
            "location": None, "fetched_at": None, "robots_allowed": None,
            "error": None if exists else "no saved file (offline reconstruction)",
        })
    return {"mode": "reconstructed-offline", "user_agent": USER_AGENT,
            "delay_seconds": DELAY_SECONDS, "started_at": None, "robots": None,
            "stopped": None, "requests": requests_log}


# ---------------------------------------------------------------- text pipeline


class _TagStripper(HTMLParser):
    """Strip tags, keep every text node (script/style included), decode entities once.

    Tags are removed without inserting anything, so "Using <a>eAccounts</a>, students"
    becomes "Using eAccounts, students". Comments and declarations are dropped.
    With skip_script_like=True, text inside script/style/noscript/template is
    dropped (diagnostic only; never used for the server-rendered gate).
    """

    SCRIPT_LIKE = {"script", "style", "noscript", "template"}

    def __init__(self, skip_script_like: bool = False):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = skip_script_like
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if self.skip and tag in self.SCRIPT_LIKE:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.skip and tag in self.SCRIPT_LIKE and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def normalize_text(text: str) -> str:
    """NFC, collapse all Unicode whitespace runs to one space, trim."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def gate_text(markup: str, skip_script_like: bool = False) -> str:
    """The server-rendered gate pipeline: strip tags -> decode entities once -> NFC -> collapse -> trim."""
    stripper = _TagStripper(skip_script_like)
    stripper.feed(markup)
    stripper.close()
    return normalize_text("".join(stripper.parts))


def el_text(el: Tag | None) -> str:
    return normalize_text(el.get_text(" ")) if el is not None else ""


def decode_html(raw: bytes) -> tuple[str, str | None]:
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return raw.decode("utf-8", errors="replace"), f"not valid UTF-8: {exc}"


# ---------------------------------------------------------------- article analysis


def dom_path(el: Tag) -> str:
    chain = []
    for node in [el, *el.parents]:
        if not isinstance(node, Tag) or node.name == "[document]":
            continue
        label = node.name
        if node.get("id"):
            label += f"#{node['id']}"
        elif node.get("class"):
            label += "." + ".".join(node["class"])
        chain.append(label)
    return " > ".join(reversed(chain))


def is_inside(el: Tag | None, container: Tag | None) -> bool:
    if el is None or container is None:
        return False
    return el is container or any(p is container for p in el.parents)


def headings(root: Tag | None) -> list[list[str]]:
    if root is None:
        return []
    return [[h.name, el_text(h)] for h in root.find_all(["h1", "h2", "h3", "h4"])]


def section_headings(body: Tag | None) -> list[str]:
    """Top-level section titles: panel titles if the body uses panels, else h2-h4."""
    if body is None:
        return []
    panel_titles = body.select(".panel-heading .panel-title")
    found = panel_titles or body.find_all(["h2", "h3", "h4"])
    return [el_text(h) for h in found]


def classify_pattern(titles: list[str]) -> str:
    """Combined labels are split on '/', so 'Issue/Question' counts as both 'issue' and 'question'."""
    names = {part.strip() for t in titles for part in t.lower().rstrip(":").split("/")}
    if {"question", "answer"} <= names:
        return "Question/Answer"
    if {"issue", "resolution"} <= names:
        present = [n for n in ("issue", "environment", "cause", "resolution") if n in names]
        return "Issue/Environment/Cause/Resolution" + ("" if len(present) == 4 else f" (partial: {'/'.join(present)})")
    if titles:
        return "other"
    return "none"


def meta_values(soup: BeautifulSoup) -> dict:
    out = {}
    for prop in META_PROPERTIES:
        tag = soup.find("meta", attrs={"property": prop})
        out[prop] = normalize_text(tag["content"]) if tag and tag.has_attr("content") else None
    out["article:tag"] = [normalize_text(t["content"]) for t in soup.find_all("meta", attrs={"property": "article:tag"})
                          if t.has_attr("content")]
    return out


def body_element(soup: BeautifulSoup, variant: str) -> tuple[Tag | None, int]:
    selector = NORMAL_BODY_SELECTOR if variant == "normal" else PRINT_BODY_SELECTOR
    matches = soup.select(selector)
    if not matches:
        return None, 0
    body = matches[0]
    if variant == "print":
        # Work on a copy with the "Body" label removed so the saved soup stays intact.
        body = BeautifulSoup(str(body), "html.parser").select_one(selector)
        label = body.find("div", class_="gutter-bottom", recursive=False)
        if label is not None and el_text(label) == "Body":
            label.decompose()
    return body, len(matches)


def analyze_page(markup: str, variant: str, phrase_norm: str | None) -> tuple[dict, str]:
    soup = BeautifulSoup(markup, "html.parser")
    body, body_matches = body_element(soup, variant)
    body_text = el_text(body)
    titles = section_headings(body)

    related_sel = NORMAL_RELATED_SELECTOR if variant == "normal" else PRINT_RELATED_SELECTOR
    related = soup.select_one(related_sel)
    related_links = related.select("a[href*='/KB/Article/']") if related else []
    original_body = soup.select_one(NORMAL_BODY_SELECTOR if variant == "normal" else PRINT_BODY_SELECTOR)

    result = {
        "title_tag": el_text(soup.title) or None,
        "h1": [el_text(h) for h in soup.find_all("h1")],
        "headings_document": headings(soup),
        "headings_in_body": headings(body),
        "meta": meta_values(soup),
        "body": {
            "selector": NORMAL_BODY_SELECTOR if variant == "normal" else
                        f"{PRINT_BODY_SELECTOR} minus its first child div.gutter-bottom ('Body' label)",
            "matches": body_matches,
            "dom_path": dom_path(original_body) if original_body is not None else None,
            "text_length_chars": len(body_text),
            "text_length_words": len(body_text.split()),
            "phrase_in_body": (phrase_norm in body_text) if phrase_norm and body is not None else None,
            "script_tags_in_body": len(body.find_all("script")) if body is not None else None,
            "section_headings": titles,
            "heading_pattern": classify_pattern(titles),
            "contains_staff_only_marker": bool(re.search(r"staff only", body_text, re.I)),
            "contains_related_articles_text": "related articles" in body_text.lower(),
        },
        "related_articles": {
            "selector": related_sel,
            "present": related is not None,
            "heading": el_text(related.select_one(".panel-title") or related.find(["h2", "h3"])) if related else None,
            "article_link_count": len(related_links),
            "inside_body": is_inside(related, original_body),
            "dom_path": dom_path(related) if related is not None else None,
        },
    }

    if variant == "normal":
        tags_div = soup.select_one(NORMAL_TAGS_SELECTOR)
        feedback = soup.select_one(NORMAL_FEEDBACK_SELECTOR)
        result["tags_visible"] = [el_text(a) for a in tags_div.find_all("a")] if tags_div else []
        result["tags_selector"] = NORMAL_TAGS_SELECTOR if tags_div else None
        result["feedback_widget"] = {"selector": NORMAL_FEEDBACK_SELECTOR, "present": feedback is not None,
                                     "inside_body": is_inside(feedback, original_body)}
        og_url = result["meta"]["og:url"] or ""
        m = ARTICLE_ID_RE.search(og_url)
        result["article_id_from_page"] = m.group(1) if m else None
        result["article_id_source"] = "meta og:url"
    else:
        art_id = soup.select_one("#ArticleID")
        m = re.search(r"\d+", el_text(art_id)) if art_id else None
        result["article_id_from_page"] = m.group(0) if m else None
        result["article_id_source"] = "#ArticleID"
        summary = soup.select_one("#divSummary")
        if summary is not None:
            summary = BeautifulSoup(str(summary), "html.parser").select_one("#divSummary")
            label = summary.find("div", class_="gutter-bottom", recursive=False)
            if label is not None:
                label.decompose()
        result["summary_text"] = el_text(summary) or None
        attrs = {}
        for group in soup.select(".js-custom-attributes-container .form-group"):
            label = group.find("span", class_="control-label")
            values = group.find_all("div", class_="wrap-text", recursive=False)
            if label is not None:
                attrs[el_text(label)] = el_text(values[-1]) if values else None
        result["custom_attributes"] = attrs
        result["details_fields"] = [el_text(s) for s in soup.select("#divDetails .control-label")]
    return result, body_text


def analyze_article(item: dict, fetch_index: dict) -> dict:
    art_id = item["article_id"]
    phrase_norm = gate_text(item["phrase"]) if item["phrase"] else None
    out = {"article_id": art_id, "url": item["url"], "phrase": item["phrase"],
           "phrase_normalized": phrase_norm, "errors": []}
    body_texts = {}
    for variant, suffix, kind in (("normal", "", "article"), ("print", "_print", "print")):
        path = HTML_DIR / f"{art_id}{suffix}.html"
        entry = fetch_index.get((kind, art_id), {})
        page = {"saved_path": path.relative_to(ROOT).as_posix(), "http_status": entry.get("status"),
                "fetch_error": entry.get("error")}
        if not path.exists():
            page["available"] = False
            out["errors"].append(f"{variant}: no saved HTML")
            out[variant] = page
            continue
        raw = path.read_bytes()
        markup, decode_error = decode_html(raw)
        page["available"] = True
        page["bytes"] = len(raw)
        page["decode_error"] = decode_error
        page_text = gate_text(markup)
        visible_text = gate_text(markup, skip_script_like=True)
        found = bool(phrase_norm) and phrase_norm in page_text
        page["server_rendered"] = found
        page["phrase_only_in_script"] = found and phrase_norm not in visible_text
        analysis, body_texts[variant] = analyze_page(markup, variant, phrase_norm)
        page.update(analysis)
        page["article_id_matches_url"] = page["article_id_from_page"] == art_id
        out[variant] = page
    out["server_rendered"] = out.get("normal", {}).get("server_rendered", False)
    if "normal" in body_texts and "print" in body_texts:
        a, b = body_texts["normal"], body_texts["print"]
        out["body_text_identical_normal_vs_print"] = a == b
        out["body_length_diff_chars"] = len(b) - len(a)
    return out


# ---------------------------------------------------------------- category analysis


def analyze_category(item: dict, fetch_index: dict) -> dict:
    cat_id = item["category_id"]
    path = CATEGORY_HTML_DIR / f"{cat_id}.html"
    entry = fetch_index.get(("category", cat_id), {})
    out = {"category_id": cat_id, "url": item["url"],
           "saved_path": path.relative_to(ROOT).as_posix(),
           "http_status": entry.get("status"), "fetch_error": entry.get("error")}
    if not path.exists():
        out["available"] = False
        return out
    raw = path.read_bytes()
    markup, decode_error = decode_html(raw)
    soup = BeautifulSoup(markup, "html.parser")
    out.update({"available": True, "bytes": len(raw), "decode_error": decode_error})

    all_ids = {m.group(1) for a in soup.find_all("a", href=True)
               if (m := ARTICLE_HREF_RE.search(a["href"]))}
    main = soup.select_one("#divCats")
    listing = []
    if main is not None:
        for a in main.select("h3.gutter-bottom-xs > a[href]"):
            m = ARTICLE_HREF_RE.search(a["href"])
            if not m:
                continue
            h3 = a.parent
            summary = h3.find_next_sibling("div", class_="gray")
            listing.append({"id": m.group(1), "title": el_text(a), "href": a["href"],
                            "summary": el_text(summary) or None})
    listing_ids = [x["id"] for x in listing]

    def header_count(label: str) -> int | None:
        for h in soup.find_all("h2"):
            m = re.fullmatch(rf"{label} \((\d+)\)", el_text(h))
            if m:
                return int(m.group(1))
        return None

    subcats = []
    for a in soup.select("h3.category-title a[href]"):
        m = CATEGORY_ID_RE.search(a["href"])
        subcats.append({"id": m.group(1) if m else None, "name": el_text(a), "href": a["href"]})
    other_category_links = sorted({a["href"] for a in soup.find_all("a", href=True)
                                   if CATEGORY_ID_RE.search(a["href"])
                                   and CATEGORY_ID_RE.search(a["href"]).group(1) not in {s["id"] for s in subcats} | {cat_id}})

    sidebar_ids = set()
    for sel in ("#ctl00_ctl00_cpContent_cpContent_divPopular", "#ctl00_ctl00_cpContent_cpContent_divRecent"):
        panel = soup.select_one(sel)
        if panel is not None:
            sidebar_ids |= {m.group(1) for a in panel.find_all("a", href=True)
                            if (m := ARTICLE_HREF_RE.search(a["href"]))}

    postback_targets = sorted(set(re.findall(r"__doPostBack\((?:'|&#39;)([^'&]+)", markup))
                              | set(re.findall(r"WebForm_PostBackOptions\((?:&quot;|\")([^&\"]+)", markup)))
    pager_like = [dom_path(el) for el in soup.find_all(True, class_=re.compile(r"pag(er|ination)", re.I))]
    page_query_links = sorted({a["href"] for a in soup.find_all("a", href=True)
                               if re.search(r"[?&](page|pageindex|p)=", a["href"], re.I)})
    js_links = sorted({el_text(a) or a.get("aria-label") or a["href"][:40]
                       for a in soup.find_all("a", href=True) if a["href"].startswith("javascript:")})

    listed_header = header_count("Articles")
    out.update({
        "title_tag": el_text(soup.title) or None,
        "h1": [el_text(h) for h in soup.find_all("h1")],
        "article_links_in_raw_html": bool(all_ids),
        "unique_article_ids_anywhere": len(all_ids),
        "listing_selector": "#divCats h3.gutter-bottom-xs > a[href*='/KB/Article/']",
        "listing_count": len(listing),
        "listing_unique_ids": len(set(listing_ids)),
        "listing_duplicate_ids": sorted({i for i in listing_ids if listing_ids.count(i) > 1}),
        "articles_header_count": listed_header,
        "listing_matches_header_count": listed_header == len(listing) if listed_header is not None else None,
        "titles_accompany_urls": bool(listing) and all(x["title"] for x in listing),
        "summaries_accompany_urls": sum(1 for x in listing if x["summary"]),
        "href_style": sorted({"absolute" if x["href"].startswith("http") else "relative" for x in listing}),
        "example_articles": listing[:5],
        "sidebar_only_article_ids": sorted(sidebar_ids - set(listing_ids), key=int),
        "ids_outside_listing_and_sidebar": sorted(all_ids - set(listing_ids) - sidebar_ids, key=int),
        "subcategories_header_count": header_count("Categories"),
        "subcategories": subcats,
        "other_category_links": other_category_links,
        "pagination": {
            "pager_elements": pager_like,
            "page_query_links": page_query_links,
            "postback_targets": postback_targets,
            "javascript_links": js_links,
        },
    })
    return out


# ---------------------------------------------------------------- summaries


def robots_details(fetch: dict) -> dict:
    robots = fetch.get("robots") or {}
    text = robots.get("text")
    if not text:
        return {"crawl_delay": None}
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    return {"crawl_delay": parser.crawl_delay(USER_AGENT)}


def template_counts(articles: list[dict]) -> list[dict]:
    templates: dict[str, list[str]] = {}
    for a in articles:
        key = " / ".join(a["normal"]["body"]["section_headings"]) or "(no section headings)"
        templates.setdefault(key, []).append(a["article_id"])
    return [{"section_headings": k, "article_ids": v}
            for k, v in sorted(templates.items(), key=lambda kv: (-len(kv[1]), kv[0]))]


def summarize_articles(articles: list[dict]) -> dict:
    ok = [a for a in articles if a.get("normal", {}).get("available")]
    both = [a for a in ok if a.get("print", {}).get("available")]

    def all_(pred, items):
        return all(pred(a) for a in items)

    def meta_presence(variant: str) -> dict:
        items = [a for a in ok if a.get(variant, {}).get("available")]
        return {prop: f"{sum(1 for a in items if a[variant]['meta'][prop])}/{len(items)}"
                for prop in META_PROPERTIES} | {
            "article:tag": f"{sum(1 for a in items if a[variant]['meta']['article:tag'])}/{len(items)}"}

    patterns: dict[str, list[str]] = {}
    for a in ok:
        patterns.setdefault(a["normal"]["body"]["heading_pattern"], []).append(a["article_id"])
    section_title_counts: dict[str, int] = {}
    for a in ok:
        for t in dict.fromkeys(a["normal"]["body"]["section_headings"]):
            section_title_counts[t] = section_title_counts.get(t, 0) + 1

    return {
        "articles_supplied": len(articles),
        "normal_pages_available": len(ok),
        "print_pages_available": sum(1 for a in articles if a.get("print", {}).get("available")),
        "all_server_rendered": bool(articles) and all_(lambda a: a["server_rendered"], articles),
        "server_rendered_failures": [a["article_id"] for a in articles if not a["server_rendered"]],
        "phrase_only_in_script": [a["article_id"] for a in ok if a["normal"]["phrase_only_in_script"]],
        "print_server_rendered_failures": [a["article_id"] for a in both if not a["print"]["server_rendered"]],
        "normal_body_selector_single_match_all": all_(lambda a: a["normal"]["body"]["matches"] == 1, ok),
        "normal_body_contains_phrase_all": all_(lambda a: a["normal"]["body"]["phrase_in_body"], ok),
        "normal_body_excludes_related_all": all_(lambda a: not a["normal"]["related_articles"]["inside_body"], ok),
        "normal_body_excludes_feedback_all": all_(lambda a: not a["normal"]["feedback_widget"]["inside_body"], ok),
        "print_body_selector_single_match_all": all_(lambda a: a["print"]["body"]["matches"] == 1, both),
        "print_body_contains_phrase_all": all_(lambda a: a["print"]["body"]["phrase_in_body"], both),
        "print_body_excludes_related_all": all_(lambda a: not a["print"]["related_articles"]["inside_body"], both),
        "body_identical_normal_vs_print": [a["article_id"] for a in both if a["body_text_identical_normal_vs_print"]],
        "body_differs_normal_vs_print": [a["article_id"] for a in both if not a["body_text_identical_normal_vs_print"]],
        "related_articles_present_normal": [a["article_id"] for a in ok if a["normal"]["related_articles"]["present"]],
        "related_articles_absent_normal": [a["article_id"] for a in ok if not a["normal"]["related_articles"]["present"]],
        "article_id_matches_url_normal": all_(lambda a: a["normal"]["article_id_matches_url"], ok),
        "article_id_matches_url_print": all_(lambda a: a["print"]["article_id_matches_url"], both),
        "meta_presence_normal": meta_presence("normal"),
        "meta_presence_print": meta_presence("print"),
        "visible_tags_match_meta_tags": all_(lambda a: a["normal"]["tags_visible"] == a["normal"]["meta"]["article:tag"], ok),
        "articles_without_tags": [a["article_id"] for a in ok if not a["normal"]["meta"]["article:tag"]],
        "heading_patterns": patterns,
        "heading_templates": template_counts(ok),
        "section_title_counts": dict(sorted(section_title_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "staff_only_marker_in_body": [a["article_id"] for a in ok if a["normal"]["body"]["contains_staff_only_marker"]],
        "script_tags_in_normal_body": [a["article_id"] for a in ok if a["normal"]["body"]["script_tags_in_body"]],
        "categories_seen": sorted({a["normal"]["meta"]["article:section"] or "(none)" for a in ok}),
        "body_length_chars_range": [min((a["normal"]["body"]["text_length_chars"] for a in ok), default=0),
                                    max((a["normal"]["body"]["text_length_chars"] for a in ok), default=0)],
        "decode_errors": [a["article_id"] for a in ok if a["normal"].get("decode_error")
                          or a.get("print", {}).get("decode_error")],
    }


def summarize_categories(categories: list[dict]) -> dict:
    ok = [c for c in categories if c.get("available")]
    works = bool(ok) and len(ok) == len(categories) and all(
        c["article_links_in_raw_html"] and c["listing_count"] > 0 and c["listing_matches_header_count"] for c in ok)
    return {
        "categories_supplied": len(categories),
        "pages_available": len(ok),
        "discovery_works_with_plain_http": works,
        "listing_matches_header_count_all": all(c["listing_matches_header_count"] for c in ok),
        "any_pager_elements": any(c["pagination"]["pager_elements"] for c in ok),
        "any_page_query_links": any(c["pagination"]["page_query_links"] for c in ok),
        "postback_targets_seen": sorted({t for c in ok for t in c["pagination"]["postback_targets"]}),
        "subcategory_links_total": sum(len(c["subcategories"]) for c in ok),
    }


# ---------------------------------------------------------------- report rendering


def md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def render_markdown(report: dict) -> str:
    fetch, asum, csum = report["fetch"], report["article_summary"], report["category_summary"]
    flags = report["flags"]
    L: list[str] = []
    add = L.append
    add("# KB Inspection Report (Phase 1)")
    add("")
    add(f"Generated by `scripts/inspect_kb.py` (analysis mode: {report['analysis_mode']}). "
        f"Fetch started {fetch.get('started_at')}; see `inspection_report.json` for full detail.")
    add("")
    add("## Flags")
    add("")
    add("Server-rendered gate: raw HTML -> strip tags (markup removed, no space inserted; script/style text kept; "
        "comments dropped) -> decode entities once -> NFC -> collapse whitespace -> trim; same pipeline on the phrase; "
        "substring match on the normal article page.")
    add("")
    if flags:
        for f in flags:
            add(f"- **{f}**")
    else:
        add("- None.")
    add("")
    add("## Summary")
    add("")
    add(f"- All article bodies server-rendered: **{'yes' if asum['all_server_rendered'] else 'NO'}** "
        f"({asum['normal_pages_available']}/{asum['articles_supplied']} normal pages; gate failures: "
        f"{asum['server_rendered_failures'] or 'none'}; phrase only inside script: {asum['phrase_only_in_script'] or 'none'})")
    add(f"- Category discovery works with plain HTTP: **{'yes' if csum['discovery_works_with_plain_http'] else 'NO'}**")
    add(f"- Recommended body selector: `{NORMAL_BODY_SELECTOR}` on the normal article page "
        f"(single match in all samples: {asum['normal_body_selector_single_match_all']}; contains the supplied phrase in all: "
        f"{asum['normal_body_contains_phrase_all']}; excludes Related Articles in all: {asum['normal_body_excludes_related_all']}; "
        f"excludes feedback widget in all: {asum['normal_body_excludes_feedback_all']})")
    add(f"- Related Articles (normal page): `{NORMAL_RELATED_SELECTOR}`, in the right-hand `div.col-md-4` sidebar, "
        f"a sibling column of the body, not inside it. Selecting the body excludes it; no removal step needed.")
    add(f"- Body text identical between normal and print: {len(asum['body_identical_normal_vs_print'])}/"
        f"{asum['print_pages_available']} (differs: {asum['body_differs_normal_vs_print'] or 'none'})")
    print_has_meta = any(v.split("/")[0] != "0" for v in asum["meta_presence_print"].values())
    add("- Easier to parse cleanly: **normal page**. Its body container is a single stable ID and it carries all "
        "meta tags (section, dates, description, tags) in the same response. "
        + ("The print page has the same body text but no meta tags, and its `#divBody` needs a 'Body' label removed."
           if not print_has_meta else "The print page also carries meta tags."))
    add(f"- Heading patterns (combined labels split on '/'): "
        + "; ".join(f"{k}: {len(v)}" for k, v in asum["heading_patterns"].items()))
    for t in asum["heading_templates"]:
        add(f"  - {len(t['article_ids'])}x `{t['section_headings']}`: {t['article_ids']}")
    add(f"- Categories (article:section) seen: {', '.join(asum['categories_seen'])}")
    add(f"- robots.txt Crawl-delay: {report['robots']['crawl_delay']} (this run used {fetch.get('delay_seconds')}s)")
    add("")
    add("### Consistent across samples")
    add("")
    cons = [f"Body container `{NORMAL_BODY_SELECTOR}` (normal) and `{PRINT_BODY_SELECTOR}` (print)",
            "Related Articles outside the body container",
            "Article ID: URL path (normal, and `og:url`) and `#ArticleID` (print)"]
    for prop, v in asum["meta_presence_normal"].items():
        if v.split("/")[0] == v.split("/")[1]:
            cons.append(f"`{prop}` meta present on normal pages ({v})")
    for c in cons:
        add(f"- {c}")
    add("")
    add("### Varying across samples")
    add("")
    var = [f"Section headings / pattern (see per-article table)",
           f"Body length: {asum['body_length_chars_range'][0]}-{asum['body_length_chars_range'][1]} chars"]
    for prop, v in asum["meta_presence_normal"].items():
        if v.split("/")[0] != v.split("/")[1]:
            var.append(f"`{prop}` meta present on only {v} normal pages")
    if asum["articles_without_tags"]:
        var.append(f"Articles with no tags: {asum['articles_without_tags']}")
    for v in var:
        add(f"- {v}")
    add("")
    add("### Parsing risks")
    add("")
    for r in report["parsing_risks"]:
        add(f"- {r}")
    add("")

    add("## Article findings")
    add("")
    add("| ID | server-rendered | only in script | section (meta) | pattern | section headings | body chars (normal/print) | normal == print body | related (normal) | tags |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for a in report["articles"]:
        n, p = a.get("normal", {}), a.get("print", {})
        if not n.get("available"):
            add(f"| {a['article_id']} | NO (not fetched) | | | | | | | | |")
            continue
        add("| {id} | {sr} | {ois} | {sec} | {pat} | {heads} | {nl}/{pl} | {same} | {rel} | {tags} |".format(
            id=a["article_id"], sr="yes" if a["server_rendered"] else "**NO**",
            ois="yes" if n["phrase_only_in_script"] else "no",
            sec=md_escape(n["meta"]["article:section"] or "-"), pat=n["body"]["heading_pattern"],
            heads=md_escape(" / ".join(n["body"]["section_headings"]) or "-"),
            nl=n["body"]["text_length_chars"], pl=p.get("body", {}).get("text_length_chars", "-"),
            same="yes" if a.get("body_text_identical_normal_vs_print") else "no",
            rel=n["related_articles"]["article_link_count"] if n["related_articles"]["present"] else "absent",
            tags=md_escape(", ".join(n["meta"]["article:tag"]) or "-")))
    add("")
    for a in report["articles"]:
        n, p = a.get("normal", {}), a.get("print", {})
        add(f"### Article {a['article_id']}")
        add("")
        add(f"- URL: {a['url']}")
        add(f"- Phrase (normalized): \"{a['phrase_normalized']}\"")
        for label, page in (("Normal", n), ("Print", p)):
            if not page.get("available"):
                add(f"- {label}: not available ({page.get('fetch_error')})")
                continue
            add(f"- **{label}** (`{page['saved_path']}`, {page['bytes']} bytes)")
            add(f"  - title tag: {page['title_tag']!r}; h1: {page['h1']}")
            add(f"  - article ID from page ({page['article_id_source']}): {page['article_id_from_page']} "
                f"(matches URL: {page['article_id_matches_url']})")
            add(f"  - server_rendered: {page['server_rendered']}; phrase_only_in_script: {page['phrase_only_in_script']}")
            m = page["meta"]
            add(f"  - meta: section={m['article:section']!r}, published={m['article:published_time']!r}, "
                f"modified={m['article:modified_time']!r}, tags={m['article:tag']}")
            add(f"  - og:description: {m['og:description']!r}")
            if label == "Normal":
                add(f"  - visible tags (`{page['tags_selector']}`): {page['tags_visible']}")
            else:
                add(f"  - summary (`#divSummary`): {page['summary_text']!r}")
                add(f"  - details fields: {page['details_fields']}; custom attributes: {page['custom_attributes']}")
            b = page["body"]
            add(f"  - body: `{b['selector']}` (matches: {b['matches']}), {b['text_length_chars']} chars / "
                f"{b['text_length_words']} words; phrase in body: {b['phrase_in_body']}")
            add(f"  - body DOM path: `{b['dom_path']}`")
            add(f"  - heading pattern: {b['heading_pattern']}; section headings: {b['section_headings']}")
            add(f"  - headings in body (h1-h4): {[f'{h}: {t}' for h, t in page['headings_in_body']]}")
            add(f"  - headings in document (h1-h4): {[f'{h}: {t}' for h, t in page['headings_document']]}")
            r = page["related_articles"]
            add(f"  - Related Articles: present={r['present']}, heading={r['heading']!r}, links={r['article_link_count']}, "
                f"inside body={r['inside_body']}, path=`{r['dom_path']}`")
            if b["contains_staff_only_marker"]:
                add("  - body contains a \"STAFF ONLY\" section")
        add("")

    add("## Category findings")
    add("")
    for c in report["categories"]:
        add(f"### Category {c['category_id']}")
        add("")
        add(f"- URL: {c['url']}")
        if not c.get("available"):
            add(f"- **not available** ({c.get('fetch_error')})")
            add("")
            continue
        add(f"- Saved: `{c['saved_path']}` ({c['bytes']} bytes); title tag: {c['title_tag']!r}")
        add(f"- Article links in raw HTTP: {c['article_links_in_raw_html']}; unique article IDs anywhere on page: "
            f"{c['unique_article_ids_anywhere']}")
        add(f"- Main listing (`{c['listing_selector']}`): {c['listing_count']} links, {c['listing_unique_ids']} unique IDs; "
            f"page header says Articles ({c['articles_header_count']}); match: {c['listing_matches_header_count']}")
        add(f"- IDs only in Popular/Recent sidebar: {len(c['sidebar_only_article_ids'])}; IDs outside listing and sidebar: "
            f"{c['ids_outside_listing_and_sidebar'] or 'none'}")
        add(f"- Titles accompany URLs: {c['titles_accompany_urls']}; summaries present on "
            f"{c['summaries_accompany_urls']}/{c['listing_count']}; href style: {c['href_style']}")
        add("- Example article URLs:")
        for x in c["example_articles"]:
            add(f"  - `{x['href']}`: {x['title']!r}")
        add(f"- Subcategories (header says {c['subcategories_header_count']}): {len(c['subcategories'])} plain links"
            + (": " + ", ".join(f"{s['id']} {s['name']}" for s in c["subcategories"]) if c["subcategories"] else ""))
        add(f"- Other category links (breadcrumb/nav): {c['other_category_links']}")
        pg = c["pagination"]
        add(f"- Pagination: pager elements: {pg['pager_elements'] or 'none'}; page-query links: "
            f"{pg['page_query_links'] or 'none'}")
        add(f"- Postback targets on page: {pg['postback_targets']}")
        add(f"- javascript: links: {pg['javascript_links']}")
        add("")
    add("### Category conclusion")
    add("")
    for line in report["category_conclusion"]:
        add(f"- {line}")
    add("")
    add("## Fetch log")
    add("")
    add(f"- User-Agent: `{fetch.get('user_agent')}`; delay {fetch.get('delay_seconds')}s; no retries; redirects not followed")
    rb = fetch.get("robots") or {}
    add(f"- robots.txt: {rb.get('url')} status {rb.get('status')} ({rb.get('interpretation')}); "
        f"KB root allowed: {rb.get('kb_root_allowed')}")
    if fetch.get("stopped"):
        add(f"- **STOPPED: {fetch['stopped']}**")
    add("")
    add("| kind | id | status | bytes | robots allowed | error |")
    add("|---|---|---|---|---|---|")
    for r in fetch.get("requests", []):
        add(f"| {r['kind']} | {r['id']} | {r['status']} | {r['bytes']} | {r['robots_allowed']} | {r['error'] or ''} |")
    add("")
    return "\n".join(L)


def build_report(fetch: dict, articles_in: list[dict], categories_in: list[dict], mode: str) -> dict:
    fetch_index = {(r["kind"], r["id"]): r for r in fetch.get("requests", [])}
    articles = [analyze_article(a, fetch_index) for a in articles_in if a["article_id"]]
    categories = [analyze_category(c, fetch_index) for c in categories_in if c["category_id"]]
    asum = summarize_articles(articles)
    csum = summarize_categories(categories)
    robots = robots_details(fetch)

    flags = []
    if fetch.get("stopped"):
        flags.append(f"RUN STOPPED: {fetch['stopped']}")
    if not asum["all_server_rendered"]:
        flags.append(f"SERVER-RENDERED TEST FAILED for article(s): {asum['server_rendered_failures']}. "
                     "Stop at reporting; no Playwright, no architecture change.")
    if asum["phrase_only_in_script"]:
        flags.append(f"Phrase found only inside script/style content for: {asum['phrase_only_in_script']} (diagnostic)")
    if not csum["discovery_works_with_plain_http"]:
        flags.append("CATEGORY DISCOVERY FAILED with plain HTTP. Stop at reporting; no Playwright, no architecture change.")
    bad_invalid = [a for a in articles_in if not a["article_id"] or a["phrase"] is None]
    if bad_invalid:
        flags.append(f"Unparseable input lines in urls.txt: {[a['line'] for a in bad_invalid]}")
    failed = [f"{r['kind']} {r['id']}: {r['error']}" for r in fetch.get("requests", []) if r.get("error")]
    if failed:
        flags.append(f"Request failures/skips: {failed}")
    if robots["crawl_delay"] and robots["crawl_delay"] > fetch.get("delay_seconds", 0):
        flags.append(f"robots.txt sets Crawl-delay: {robots['crawl_delay']}. This Phase 1 run used "
                     f"{fetch.get('delay_seconds')}s ({sum(1 for r in fetch.get('requests', []) if r.get('fetched_at'))} "
                     f"page requests) before this was known. Future collection will honor the "
                     f"{robots['crawl_delay']}-second Crawl-delay (human decision; project docs updated by the human).")

    risks = []
    if asum["staff_only_marker_in_body"]:
        risks.append(f"PHASE 2 PARSING DECISION (not decided in Phase 1): an \"ITS STAFF ONLY\" section sits inside "
                     f"the body container in {asum['staff_only_marker_in_body']}. It is publicly visible (human-checked "
                     "logged out on 233) and is a list of links to technician articles. The body selector currently "
                     "includes it. Likely handling: store it as a separate field rather than merging it into body "
                     "text, so including or excluding it can be measured later.")
    other = asum["heading_patterns"].get("other", [])
    if other:
        risks.append(f"{len(other)} article(s) use section headings other than Question/Answer or "
                     f"Issue/Environment/Cause/Resolution: {other}. Section-field mapping needs a rule for them.")
    if "Question/Answer" not in asum["heading_patterns"]:
        risks.append("No sample uses a Question/Answer pair: 'Question' appears only with 'Products or Services "
                     "Involved / Instructions / Need More Help?', with no 'Answer' heading.")
    if any(v.startswith("Issue/Environment/Cause/Resolution") for v in asum["heading_patterns"]):
        risks.append("The Issue section is labelled 'Issue/Question' (a combined label), and 'Cause' is absent "
                     "from some articles; the parser must treat both as normal.")
    no_body_phrase = [a["article_id"] for a in articles if a.get("normal", {}).get("available")
                      and a["normal"]["body"]["phrase_in_body"] is False]
    if no_body_phrase:
        risks.append(f"Body text extracted with a space per element boundary (BeautifulSoup get_text(' ')) puts "
                     f"a space before punctuation that follows an inline tag, so the supplied phrase does not "
                     f"match in {no_body_phrase}. Phase 2 body extraction should not insert spaces at inline "
                     "element boundaries (a, strong, em, span).")
    if asum["articles_without_tags"]:
        risks.append(f"Some articles have no tags: {asum['articles_without_tags']}.")
    if asum["body_differs_normal_vs_print"]:
        risks.append(f"Normal and print body text differ for {asum['body_differs_normal_vs_print']}.")
    if asum["script_tags_in_normal_body"]:
        risks.append(f"<script> inside the body container for {asum['script_tags_in_normal_body']}.")
    risks.append("Body selector is an ASP.NET WebForms control ID (ctl00_ctl00_cpContent_cpContent_divBody). "
                 "Stable across these samples, but a TeamDynamix upgrade could rename it; the collector's "
                 "validity check should fail loudly when it matches zero or multiple elements.")
    risks.append("Section headings are free-text panel titles written by KB authors, not a fixed schema; "
                 "match them by normalized text, not by position.")
    if any(a.get("print", {}).get("available") and not a["print"]["meta"]["article:section"] for a in articles):
        risks.append("Print pages carry no meta tags (no article:section, dates, or tags). "
                     "Category and tags must come from the normal page.")

    concl = []
    if csum["discovery_works_with_plain_http"]:
        concl.append("Each category page lists all of its direct articles in raw HTTP as plain <a> links with titles "
                     "and summaries; the listing count equals the page's 'Articles (N)' header on every sample.")
        if not csum["any_pager_elements"] and not csum["any_page_query_links"]:
            concl.append("No pager elements and no page-query links. Pagination is not used: the full list is on one page.")
        concl.append(f"Postbacks present only for: {csum['postback_targets_seen']} (subscribe/search), "
                     "not for listing or navigation.")
        concl.append("Subcategories are plain <a href='/KB/Category/<id>/...'> links; their articles are NOT inlined, "
                     "so full collection must walk subcategories recursively and dedupe article IDs.")
        concl.append("Discovery looks stable enough for full collection with plain HTTP.")
    else:
        concl.append("Discovery with plain HTTP did NOT work on every sample; see per-category details.")

    return {
        "analysis_mode": mode,
        "fetch": fetch,
        "robots": robots,
        "flags": flags,
        "article_summary": asum,
        "parsing_risks": risks,
        "category_summary": csum,
        "category_conclusion": concl,
        "articles": articles,
        "categories": categories,
    }


# ---------------------------------------------------------------- main


def block_network() -> None:
    def refuse(*_args, **_kwargs):
        raise RuntimeError("--offline: network access attempted")
    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="analyze already-saved HTML; make zero network requests")
    args = ap.parse_args()

    articles = read_article_inputs()
    categories = read_category_inputs()
    targets = build_targets(articles, categories)

    if args.offline:
        block_network()
        fetch = load_saved_fetch_log(targets)
    else:
        fetch = fetch_all(targets)

    report = build_report(fetch, articles, categories, "offline" if args.offline else "network")
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {REPORT_JSON.relative_to(ROOT).as_posix()} and {REPORT_MD.relative_to(ROOT).as_posix()}")
    for flag in report["flags"]:
        print(f"FLAG: {flag}")
    asum, csum = report["article_summary"], report["category_summary"]
    print(f"server-rendered: {asum['articles_supplied'] - len(asum['server_rendered_failures'])}/"
          f"{asum['articles_supplied']}; category discovery via plain HTTP: {csum['discovery_works_with_plain_http']}")
    gate_ok = asum["all_server_rendered"] and csum["discovery_works_with_plain_http"] and not fetch.get("stopped")
    return 0 if gate_ok else 1


if __name__ == "__main__":
    sys.exit(main())
