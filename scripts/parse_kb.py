#!/usr/bin/env python
"""Phase 2B: deterministic parser for saved raw KB article HTML.

Input : data/raw/articles/<id>.html (raw bytes from collect_kb.py)
Output: data/parsed/articles.jsonl   (one article per line, sorted by ID)
        data/parsed/parse_report.json (counts, failures, warnings per ID)

Run:  .venv/Scripts/python scripts/parse_kb.py

Rules
-----
Body scope   Only #ctl00_ctl00_cpContent_cpContent_divBody on the normal page.
             Related Articles and feedback sit outside it. Print pages are unused.

Text         Own tree walker. Inline tags (a, b, strong, em, span, ...) add no
             characters, so "<a>eAccounts</a>, students" stays "eAccounts, students".
             Block tags and <br> end a line. Whitespace runs (incl. NBSP) collapse to
             one space; zero-width characters (U+200B-D, U+2060, U+FEFF) are dropped.
             No list markers or other characters are invented.

Sections     Heading units are direct-child <h2> (flat layout) or .panel-title
             (panel layout). <h3> sub-headings stay inside their section. Content
             before the first heading is a heading-less preamble section.
             Heading text is normalized (casefold, collapse whitespace, spaces around
             "/" removed, trailing ":" or "?" removed), then mapped:
               issue/question, issue -> issue      question -> question
               answer -> answer                    environment -> environment
               cause -> cause                      resolution -> resolution
               anything else (Products or Services Involved, Instructions,
               Need More Help?, custom) -> only in the ordered `sections` list.
             "Issue/Question" is the issue field; `question` is set only from a bare
             "Question" heading. A missing section is null (never invented). A
             heading with no text also gives null; the heading stays in `sections`.
             A repeated canonical heading concatenates in document order.

ITS STAFF ONLY
             Marker = outermost element in the body whose whole text (casefold,
             whitespace collapsed, trailing ":" stripped) is "its staff only".
             Container: if the marker is (inside) a .panel-heading, the enclosing
             .panel; else if the marker is the first non-blank child of its parent and
             that parent is neither the body root nor a <footer>, that parent.
             The container is removed before sectioning and fullText and reported in
             `staffOnly` {text (marker excluded), links}. A marker with no bounded
             container, or "staff only" text left in the body, leaves the body
             untouched and adds a warning (staff_only_unbounded / staff_only_unmatched).

Footer       Every <footer> in the body (after staff removal) is removed from
             fullText and the sections and preserved in `footer`. If its text is not
             exactly the standard service-desk line, warning footer_nonstandard.

fullText     Body text: preamble + each heading line + its content, without the
             staff-only block and without footers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
PARSER_VERSION = "1"
BODY_SELECTOR = "#ctl00_ctl00_cpContent_cpContent_divBody"

WS_RE = re.compile(r"[ \t\r\n\f\v ]+")
INVISIBLE_RE = re.compile("[" + "".join(map(chr, (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))) + "]")  # zero-width
ARTICLE_ID_RE =re.compile(r"/KB/Article(?:Det)?/(\d+)(?:/|$)|[?&]ID=(\d+)")
SKIP_TAGS = {"script", "style", "noscript", "template", "head", "img", "svg"}
BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "blockquote", "pre",
    "section", "article", "aside", "nav", "header", "footer", "hr", "br", "form",
    "fieldset", "legend", "figure", "figcaption", "address", "details", "summary",
}
CANONICAL = {
    "issue/question": "issue", "issue": "issue", "question": "question",
    "answer": "answer", "environment": "environment", "cause": "cause",
    "resolution": "resolution",
}
STANDARD_FOOTER_RE = re.compile(
    r"^please contact the service desk( if you need further assistance)?\.?$")


class ParseError(Exception):
    pass


# ------------------------------------------------------------------ text


def _collapse(s: str) -> str:
    return WS_RE.sub(" ", INVISIBLE_RE.sub("", s)).strip()


def lines_of(node) -> list[str]:
    """Deterministic text lines of a node (inline adds nothing, block breaks)."""
    lines: list[str] = []
    buf: list[str] = []

    def flush():
        text = _collapse("".join(buf))
        buf.clear()
        if text:
            lines.append(text)

    def walk(n):
        if isinstance(n, Comment):
            return
        if isinstance(n, NavigableString):
            if type(n) is NavigableString:  # not Doctype/CData/etc.
                buf.append(str(n))
            return
        if not isinstance(n, Tag) or n.name in SKIP_TAGS:
            return
        block = n.name in BLOCK_TAGS
        if block:
            flush()
        for child in n.children:
            walk(child)
        if block:
            flush()

    walk(node)
    flush()
    return [unicodedata.normalize("NFC", ln) for ln in lines]


def text_of(node) -> str:
    return "\n".join(lines_of(node))


def norm_key(s: str) -> str:
    s = _collapse(s).casefold()
    return re.sub(r"\s*/\s*", "/", s).rstrip(":?").strip()


# ------------------------------------------------------------------ metadata


def _meta_all(soup: BeautifulSoup, key: str) -> list[str]:
    out = []
    for m in soup.find_all("meta"):
        if m.get("property") == key or m.get("name") == key:
            c = m.get("content")
            if c is not None:
                out.append(_collapse(c))
    return out


def _meta(soup, key):
    vals = _meta_all(soup, key)
    return vals[0] if vals and vals[0] else None


def _article_id(url: str | None) -> str | None:
    if not url:
        return None
    m = ARTICLE_ID_RE.search(url)
    return (m.group(1) or m.group(2)) if m else None


# ------------------------------------------------------------------ staff only / footer


def _first_content_child(parent: Tag):
    for c in parent.children:
        if isinstance(c, Comment):
            continue
        if isinstance(c, NavigableString) and not str(c).strip():
            continue
        return c
    return None


def _closest_with_class(node: Tag, cls: str, stop: Tag) -> Tag | None:
    cur = node
    while cur is not None and cur is not stop:
        if isinstance(cur, Tag) and cls in (cur.get("class") or []):
            return cur
        cur = cur.parent
    return None


def _find_staff_markers(body: Tag) -> list[Tag]:
    markers: list[Tag] = []
    for el in body.find_all(True):
        if any(m in el.parents for m in markers):
            continue  # inside an already-chosen (outermost) marker
        if norm_key(text_of(el)) == "its staff only":
            markers.append(el)
    return markers


def extract_staff_only(body: Tag, warnings: list[str]) -> dict | None:
    texts: list[str] = []
    links: list[dict] = []
    unbounded = False
    for marker in _find_staff_markers(body):
        container = None
        heading = _closest_with_class(marker, "panel-heading", body)
        if heading is not None:
            container = _closest_with_class(heading.parent, "panel", body) if heading.parent else None
        else:
            parent = marker.parent
            if (parent is not None and parent is not body and parent.name != "footer"
                    and _first_content_child(parent) is marker):
                container = parent
        if container is None:
            unbounded = True
            continue
        marker.decompose()
        t = text_of(container)
        if t:
            texts.append(t)
        for a in container.find_all("a", href=True):
            links.append({"text": _collapse(a.get_text()), "href": a["href"].strip()})
        container.decompose()
    if unbounded:
        warnings.append("staff_only_unbounded")
    elif "staff only" in norm_key(text_of(body)).replace("\n", " "):
        warnings.append("staff_only_unmatched")
    if not texts and not links:
        return None
    return {"text": "\n".join(texts), "links": links}


def extract_footer(body: Tag, warnings: list[str]) -> str | None:
    texts = []
    for f in body.find_all("footer"):
        if f.parent is None:
            continue  # nested inside an already-removed footer
        t = text_of(f)
        if t:
            texts.append(t)
        f.decompose()
    if not texts:
        return None
    joined = "\n".join(texts)
    if not STANDARD_FOOTER_RE.match(norm_key(joined).replace("\n", " ")):
        warnings.append("footer_nonstandard")
    return joined


# ------------------------------------------------------------------ sections


def split_sections(body: Tag) -> list[dict]:
    """Ordered [{heading (str|None), lines}] from the body's direct children."""
    units: list[dict] = [{"heading": None, "lines": []}]
    for child in body.children:
        if not isinstance(child, Tag):
            if type(child) is NavigableString and child.strip():
                units[-1]["lines"].extend(lines_of(child))
            continue
        if child.name == "h2":
            units.append({"heading": _collapse(child.get_text()), "lines": []})
            continue
        if "panel" in (child.get("class") or []):
            title = child.select_one(".panel-heading .panel-title")
            if title is not None:
                units.append({"heading": _collapse(title.get_text()), "lines": []})
                for part in child.children:
                    if isinstance(part, Tag) and "panel-heading" in (part.get("class") or []):
                        continue
                    units[-1]["lines"].extend(lines_of(part))
                continue
        units[-1]["lines"].extend(lines_of(child))
    if not units[0]["lines"]:
        units.pop(0)
    return units


# ------------------------------------------------------------------ main entry


def parse_article(raw: bytes, url: str | None = None) -> dict:
    try:
        markup = raw.decode("utf-8")
    except UnicodeDecodeError:
        markup = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(markup, "html.parser")
    bodies = soup.select(BODY_SELECTOR)
    if len(bodies) != 1:
        raise ParseError(f"body selector matched {len(bodies)} elements (expected 1)")
    body = bodies[0]
    warnings: list[str] = []

    canonical_url = _meta(soup, "og:url") or url
    sid = _article_id(canonical_url)
    if sid is None:
        raise ParseError("could not determine source article ID")
    if url and _article_id(url) not in (None, sid):
        warnings.append("id_mismatch_url_vs_og_url")
    title = _meta(soup, "og:title")
    if not title:
        h1 = soup.find("h1")
        title = _collapse(h1.get_text()) if h1 else None
        if not title:
            raise ParseError("no title found")
        warnings.append("title_from_h1")
    tags: list[str] = []
    for t in _meta_all(soup, "article:tag"):
        if t and t not in tags:
            tags.append(t)

    staff_only = extract_staff_only(body, warnings)
    footer = extract_footer(body, warnings)
    units = split_sections(body)

    full_lines: list[str] = []
    sections: list[dict] = []
    fields: dict[str, str] = {}
    for u in units:
        text = "\n".join(u["lines"])
        if u["heading"]:
            full_lines.append(u["heading"])
        full_lines.extend(u["lines"])
        sections.append({"heading": u["heading"], "text": text})
        field = CANONICAL.get(norm_key(u["heading"])) if u["heading"] else None
        if field:
            fields[field] = f"{fields[field]}\n{text}" if field in fields and text else (
                fields.get(field) or text)
    full_text = unicodedata.normalize("NFC", "\n".join(full_lines))
    if not full_text:
        raise ParseError("body text is empty")

    return {
        "sourceArticleId": sid,
        "title": title,
        "url": canonical_url,
        "category": _meta(soup, "article:section"),
        "tags": tags,
        "fullText": full_text,
        "question": fields.get("question") or None,
        "answer": fields.get("answer") or None,
        "issue": fields.get("issue") or None,
        "environment": fields.get("environment") or None,
        "cause": fields.get("cause") or None,
        "resolution": fields.get("resolution") or None,
        "sections": sections,
        "staffOnly": staff_only,
        "footer": footer,
        "publishedTime": _meta(soup, "article:published_time"),
        "modifiedTime": _meta(soup, "article:modified_time"),
        "description": _meta(soup, "og:description"),
        "parserVersion": PARSER_VERSION,
        "warnings": warnings,
    }


def dumps(article: dict) -> str:
    return json.dumps(article, ensure_ascii=False, sort_keys=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input-dir", type=Path, default=ROOT / "data" / "raw" / "articles")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "parsed" / "articles.jsonl")
    ap.add_argument("--report", type=Path, default=ROOT / "data" / "parsed" / "parse_report.json")
    args = ap.parse_args(argv)

    files = sorted(args.input_dir.glob("*.html"),
                   key=lambda p: (int(p.stem) if p.stem.isdigit() else 10**12, p.name))
    records, failures, warn_by_id = [], [], {}
    for f in files:
        try:
            art = parse_article(f.read_bytes())
        except ParseError as exc:
            failures.append({"file": f.name, "error": str(exc)})
            continue
        if f.stem.isdigit() and art["sourceArticleId"] != f.stem:
            failures.append({"file": f.name,
                             "error": f"og:url ID {art['sourceArticleId']} != filename ID"})
            continue
        if art["warnings"]:
            warn_by_id[art["sourceArticleId"]] = art["warnings"]
        records.append(art)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(dumps(a) + "\n" for a in records), encoding="utf-8", newline="\n")
    counts: dict[str, int] = {}
    for ws in warn_by_id.values():
        for w in ws:
            counts[w] = counts.get(w, 0) + 1
    report = {
        "parserVersion": PARSER_VERSION,
        "files": len(files),
        "parsed": len(records),
        "failed": len(failures),
        "withStaffOnly": sum(1 for a in records if a["staffOnly"]),
        "withFooter": sum(1 for a in records if a["footer"]),
        "warningCounts": dict(sorted(counts.items())),
        "warningsById": dict(sorted(warn_by_id.items(), key=lambda kv: int(kv[0]))),
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8", newline="\n")
    print(f"files {len(files)}  parsed {len(records)}  failed {len(failures)}  "
          f"staffOnly {report['withStaffOnly']}  footer {report['withFooter']}")
    for f in failures:
        print(f"  FAIL {f['file']}: {f['error']}")
    for i, ws in report["warningsById"].items():
        print(f"  WARN {i}: {', '.join(ws)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
