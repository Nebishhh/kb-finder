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
             Also a marker: the .panel-title of a .panel that is a direct child of the
             body when its normalized text is "internal staff only", "internal its
             staff only" or ends with " - internal staff only"; container = that panel.
             Nested (example/documentation) panels are not matched.
             Empty box: an ITS STAFF ONLY marker that is a direct-child <div> of the body
             wrapping the label (no other text) is itself the container; it is dropped
             and staffOnly stays null. A bare <p> label in the body is still unbounded.

Audience labels
             An element whose whole text (whitespace collapsed, case-sensitive) matches
             ^[A-Z]{2,10} STAFF ONLY$ other than "ITS STAFF ONLY" (USD, HR, EPC,
             FINANCE ...) is an audience label: recorded in `audienceLabels` (document
             order, de-duplicated, [] if none) and only that label element is removed.
             The content around it stays in sections/fullText and is NOT staffOnly.
             No boundary is inferred.

Footer       1. After the staff-only block is removed, the full text of every
                outermost <footer> is preserved in `footer` (never discarded).
             2. Lines that are exactly the standard line ("Please contact the
                Service Desk [if you need further assistance]", casefold,
                whitespace/trailing ":?" normalized) are removed from all
                body-derived text (fullText, sections, canonical fields), wherever
                they appear: footer, direct paragraph, panel, or other section.
                Nothing fuzzy: near-miss Service Desk wording is kept. The `footer`
                field itself keeps the original text.
             3. Whatever footer lines remain are appended to fullText in document
                order (at the footer's position) and are NOT part of `sections` or
                the issue/resolution/... fields.
             4. Warnings: footer_nonstandard (informational: the footer was not
                only the standard line); footer_unclassified (needs review; the
                content is still kept in fullText): a remaining line mentions
                "service desk"/"help desk" but is not the standard line, a
                remaining line mentions "staff only", or the footer is not a direct
                child of the body (nested footers stay in place in their section).

fullText     Body text: preamble + each heading line + its content, in document
             order, with leftover footer lines, without the staff-only block and
             without the standard service-desk line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
PARSER_VERSION = "2"
BODY_SELECTOR = "#ctl00_ctl00_cpContent_cpContent_divBody"

WS_RE = re.compile(r"[ \t\r\n\f\v ]+")
INVISIBLE_RE = re.compile("[" + "".join(map(chr, (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))) + "]")  # zero-width
DESK_RE = re.compile(r"\b(?:service|help) desk\b", re.I)
ARTICLE_ID_RE = re.compile(r"/KB/Article(?:Det)?/(\d+)(?:/|$)|[?&]ID=(\d+)")
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
AUDIENCE_LABEL_RE = re.compile(r"^[A-Z]{2,10} STAFF ONLY$")  # case-sensitive
INTERNAL_PANEL_TITLE_RE = re.compile(r"^internal (?:its )?staff only$|.* - internal staff only$")
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


def is_standard_line(line: str) -> bool:
    return bool(STANDARD_FOOTER_RE.match(norm_key(line)))


def drop_standard(lines: list[str]) -> list[str]:
    """Remove lines that are exactly the standard service-desk line (nothing fuzzy)."""
    return [ln for ln in lines if not is_standard_line(ln)]


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


def _is_internal_panel_title(el: Tag, body: Tag) -> bool:
    if "panel-title" not in (el.get("class") or []):
        return False
    panel = _closest_with_class(el, "panel", body)
    if panel is None or panel.parent is not body:
        return False  # nested example/documentation panels are not staff-only content
    return bool(INTERNAL_PANEL_TITLE_RE.match(norm_key(text_of(el))))


def _find_staff_markers(body: Tag) -> list[Tag]:
    markers: list[Tag] = []
    for el in body.find_all(True):
        if any(m in el.parents for m in markers):
            continue  # inside an already-chosen (outermost) marker
        if norm_key(text_of(el)) == "its staff only" or _is_internal_panel_title(el, body):
            markers.append(el)
    return markers


def extract_audience_labels(body: Tag) -> list[str]:
    """Remove audience-label elements ("USD STAFF ONLY" ...) and return their texts."""
    found: list[Tag] = []
    for el in body.find_all(True):
        if any(m in el.parents for m in found):
            continue
        t = _collapse(" ".join(lines_of(el)))
        if AUDIENCE_LABEL_RE.match(t) and t != "ITS STAFF ONLY":
            found.append(el)
    labels: list[str] = []
    for el in found:
        t = _collapse(" ".join(lines_of(el)))
        if t not in labels:
            labels.append(t)
        el.decompose()
    return labels


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
            if (marker.parent is body and marker.name == "div"
                    and any(norm_key(text_of(d)) == "its staff only" for d in marker.find_all(True))):
                marker.decompose()  # empty ITS box: the label wrapper is the container
                continue
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


def process_footers(body: Tag, warnings: list[str]) -> str | None:
    """Return the full footer text (staff block already removed). Each outermost
    footer is rewritten in place to hold only its non-boilerplate lines, so they
    stay in document order. See the Footer rule in the module docstring."""
    originals: list[str] = []
    unclassified = False
    for f in body.find_all("footer"):
        if f.find_parent("footer") is not None:
            continue  # nested in an outer footer: handled as part of it
        lines = lines_of(f)
        if not lines:
            continue
        originals.append("\n".join(lines))
        keep = drop_standard(lines)
        if f.parent is not body:
            unclassified = True  # position in the article is ambiguous
        if any(DESK_RE.search(ln) for ln in keep) or any("staff only" in ln.casefold() for ln in keep):
            unclassified = True  # contact-like or staff-like text that is not the standard line
        new = Tag(name="footer")
        for ln in keep:
            p = Tag(name="p")
            p.append(NavigableString(ln))
            new.append(p)
        f.replace_with(new)
    if not originals:
        return None
    joined = "\n".join(originals)
    if not all(STANDARD_FOOTER_RE.match(norm_key(o)) for o in originals):
        warnings.append("footer_nonstandard")
    if unclassified:
        warnings.append("footer_unclassified")
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
        if child.name == "footer":
            units.append({"heading": None, "footer": True, "lines": lines_of(child)})
            units.append({"heading": None, "lines": []})  # anything after the footer
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
    for u in units:
        u["lines"] = drop_standard(u["lines"])
    return [u for u in units if u["lines"] or u["heading"]]


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

    audience_labels = extract_audience_labels(body)
    staff_only = extract_staff_only(body, warnings)
    footer = process_footers(body, warnings)
    units = split_sections(body)

    full_lines: list[str] = []
    sections: list[dict] = []
    fields: dict[str, str] = {}
    for u in units:
        if u.get("footer"):  # leftover footer content: fullText only, in document order
            full_lines.extend(u["lines"])
            continue
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
        "audienceLabels": audience_labels,
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


def _write_temp(path: Path, text: str) -> Path:
    """Write text to a sibling temp file (flushed to disk) and return its path."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def atomic_write_text(path: Path, text: str) -> None:
    tmp = _write_temp(path, text)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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

    # Candidate goes to a sibling temp file; it replaces the real output only if
    # every input parsed, so a complete corpus is never overwritten by a partial one.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    candidate = _write_temp(args.out, "".join(dumps(a) + "\n" for a in records))
    try:
        if not failures:
            os.replace(candidate, args.out)
            candidate = None
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
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
    atomic_write_text(args.report,
                      json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if failures:
        print(f"parse failed: kept existing {args.out.name} (not replaced)")
    print(f"files {len(files)}  parsed {len(records)}  failed {len(failures)}  "
          f"staffOnly {report['withStaffOnly']}  footer {report['withFooter']}")
    for f in failures:
        print(f"  FAIL {f['file']}: {f['error']}")
    for i, ws in report["warningsById"].items():
        print(f"  WARN {i}: {', '.join(ws)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
