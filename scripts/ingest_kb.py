#!/usr/bin/env python
"""Phase 4: load data/parsed/articles.jsonl into the Postgres articles table.

Input : data/parsed/articles.jsonl (one parser record per line)
Target: articles table from supabase/migrations/*_create_articles.sql
DB    : DATABASE_URL environment variable only (never argv). Hosts other than
        localhost / 127.0.0.1 / ::1 are refused unless --allow-remote is given.

Run:  .venv/Scripts/python scripts/ingest_kb.py [--input PATH] [--prune] [--allow-remote]

Rules
-----
Validation   Every record is checked before any connection is opened, and all
             errors are collected. Any error: nothing is written, exit 1.
             Errors name the record by sourceArticleId (or line number when the
             ID itself is unusable) plus the field and rule, never the value.
Upsert       One transaction, records in numeric sourceArticleId order.
             INSERT ... ON CONFLICT (source_article_id) DO UPDATE only when a
             column actually differs, so a rerun on the same file rewrites nothing.
             search_vector is a generated column; it is never computed here.
Prune        Opt-in (--prune). Without it, rows in the DB whose ID is not in the
             file are left alone and counted as db_only. With it they are deleted
             in the same transaction and counted as deleted.
Output       Counts, the input SHA-256 and error lines only. Article text is never
             printed. On a database error only the exception class, SQLSTATE and
             constraint name are printed (psycopg's message and DETAIL can
             contain row data).
Exit codes   0 ok, 1 validation failed, 2 usage/config, 3 database error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from parse_kb import PARSER_VERSION

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "parsed" / "articles.jsonl"

ID_RE = re.compile(r"^[1-9][0-9]*$")
URL_RE = re.compile(r"^https://td\.usd\.edu/TDClient/33/Portal/KB/Article/([0-9]+)/")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# articles.jsonl key -> articles column, in table order.
COLUMNS = {
    "sourceArticleId": "source_article_id",
    "title": "title",
    "url": "url",
    "category": "category",
    "tags": "tags",
    "description": "description",
    "publishedTime": "published_time",
    "modifiedTime": "modified_time",
    "fullText": "full_text",
    "question": "question",
    "answer": "answer",
    "issue": "issue",
    "environment": "environment",
    "cause": "cause",
    "resolution": "resolution",
    "sections": "sections",
    "staffOnly": "staff_only",
    "audienceLabels": "audience_labels",
    "footer": "footer",
    "parserVersion": "parser_version",
    "warnings": "warnings",
}
FIELDS = set(COLUMNS)
JSONB_KEYS = {"sections", "staffOnly"}
NULLABLE_STR = ("category", "description", "question", "answer", "issue",
                "environment", "cause", "resolution", "footer")
STR_LISTS = ("tags", "audienceLabels", "warnings")


# ------------------------------------------------------------------ validation


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _has_nul(v) -> bool:
    """Postgres text cannot hold U+0000."""
    if isinstance(v, str):
        return "\x00" in v
    if isinstance(v, list):
        return any(_has_nul(x) for x in v)
    if isinstance(v, dict):
        return any(_has_nul(k) or _has_nul(x) for k, x in v.items())
    return False


def _check_sections(v) -> str | None:
    if not isinstance(v, list):
        return "sections must be a list"
    for s in v:
        if not isinstance(s, dict) or set(s) != {"heading", "text"}:
            return "sections items must be {heading, text}"
        if not (s["heading"] is None or isinstance(s["heading"], str)):
            return "sections heading must be str or null"
        if not isinstance(s["text"], str):
            return "sections text must be str"
    return None


def _check_staff_only(v) -> str | None:
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) != {"text", "links"}:
        return "staffOnly must be null or {text, links}"
    if not isinstance(v["text"], str):
        return "staffOnly text must be str"
    if not isinstance(v["links"], list) or not all(
            isinstance(l, dict) and set(l) == {"text", "href"}
            and isinstance(l["text"], str) and isinstance(l["href"], str)
            for l in v["links"]):
        return "staffOnly links must be a list of {text: str, href: str}"
    return None


def validate_record(rec) -> list[str]:
    """Return rule violations for one parsed record. Messages never include values."""
    if not isinstance(rec, dict):
        return ["not a JSON object"]
    errors = [f"missing key {k}" for k in sorted(FIELDS - set(rec))]
    errors += [f"unexpected key {k}" for k in sorted(set(rec) - FIELDS)]
    if errors:
        return errors

    sid = rec["sourceArticleId"]
    if not (isinstance(sid, str) and ID_RE.match(sid)):
        errors.append("sourceArticleId must be a string of digits without leading zeros")
    for k in ("title", "fullText"):
        if not (isinstance(rec[k], str) and rec[k].strip()):
            errors.append(f"{k} must be a non-empty string")
    url = rec["url"]
    m = URL_RE.match(url) if isinstance(url, str) else None
    if not m:
        errors.append("url must be a td.usd.edu KB article URL")
    elif isinstance(sid, str) and m.group(1) != sid:
        errors.append("url article ID must equal sourceArticleId")
    for k in NULLABLE_STR:
        if not (rec[k] is None or isinstance(rec[k], str)):
            errors.append(f"{k} must be str or null")
    for k in ("publishedTime", "modifiedTime"):
        v = rec[k]
        if not (v is None or (isinstance(v, str) and TIME_RE.match(v))):
            errors.append(f"{k} must be null or YYYY-MM-DDTHH:MM[:SS]Z")
    for k in STR_LISTS:
        if not _is_str_list(rec[k]):
            errors.append(f"{k} must be list[str]")
    for problem in (_check_sections(rec["sections"]), _check_staff_only(rec["staffOnly"])):
        if problem:
            errors.append(problem)
    if rec["parserVersion"] != PARSER_VERSION:
        errors.append(f"parserVersion must be {PARSER_VERSION!r}")
    if _has_nul(rec):
        errors.append("contains a NUL character")
    return errors


def _label(rec, lineno: int) -> str:
    sid = rec.get("sourceArticleId") if isinstance(rec, dict) else None
    if isinstance(sid, str) and ID_RE.match(sid):
        return f"sourceArticleId={sid}"
    return f"line {lineno}"


def load_records(lines: list[str]) -> tuple[list[dict], list[str]]:
    """Parse and validate all lines. Returns (records, error lines)."""
    records, errors, seen = [], [], {}
    for n, line in enumerate(lines, 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"invalid record line {n}: not valid JSON")
            continue
        label = _label(rec, n)
        problems = validate_record(rec)
        if not problems:
            sid = rec["sourceArticleId"]
            if sid in seen:
                problems = [f"duplicate sourceArticleId (first on line {seen[sid]})"]
            else:
                seen[sid] = n
        errors += [f"invalid record {label}: {p}" for p in problems]
        if not problems:
            records.append(rec)
    if not lines:
        errors.append("input has no records")
    records.sort(key=lambda r: int(r["sourceArticleId"]))
    return records, errors


def to_row(rec: dict) -> tuple:
    """Record -> column values in COLUMNS order (JSON fields still Python objects)."""
    return tuple(rec[k] for k in COLUMNS)


# ------------------------------------------------------------------ config


def check_database_url(url: str | None, allow_remote: bool, environ) -> str | None:
    """Return an error message, or None if the URL may be used. Never echoes the URL."""
    if not url:
        return "DATABASE_URL is not set"
    from psycopg.conninfo import conninfo_to_dict
    try:
        info = conninfo_to_dict(url)
    except Exception:
        return "DATABASE_URL is not a valid connection string"
    if allow_remote:
        return None
    hosts = []
    for key in ("host", "hostaddr"):
        hosts += [h.strip() for h in str(info.get(key) or "").split(",") if h.strip()]
    if not hosts:
        hosts = [environ.get("PGHOST") or "localhost"]
    if not all(h in LOCAL_HOSTS or h.startswith("/") for h in hosts):
        return "DATABASE_URL host is not local; pass --allow-remote to use it"
    return None


# ------------------------------------------------------------------ database


def sync(conn, rows: list[tuple], prune: bool) -> dict:
    """Upsert rows and count/prune DB-only rows. Caller owns the transaction."""
    from psycopg.types.json import Jsonb

    cols = list(COLUMNS.values())
    jsonb_idx = {i for i, k in enumerate(COLUMNS) if k in JSONB_KEYS}
    data_cols = cols[1:]
    upsert = (
        f"insert into articles ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
        f"on conflict (source_article_id) do update set "
        f"{', '.join(f'{c} = excluded.{c}' for c in data_cols)} "
        f"where ({', '.join(f'articles.{c}' for c in data_cols)}) is distinct from "
        f"({', '.join(f'excluded.{c}' for c in data_cols)}) "
        f"returning (xmax = 0) as inserted"
    )
    inserted = updated = 0
    ids = [r[0] for r in rows]
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(upsert, [Jsonb(v) if i in jsonb_idx and v is not None else v
                                 for i, v in enumerate(r)])
            got = cur.fetchone()
            if got is not None:
                if got[0]:
                    inserted += 1
                else:
                    updated += 1
        deleted = 0
        if prune:
            cur.execute("delete from articles where source_article_id <> all(%s)", [ids])
            deleted = cur.rowcount
        cur.execute("select count(*) from articles where source_article_id <> all(%s)", [ids])
        db_only = cur.fetchone()[0]
    return {"inserted": inserted, "updated": updated,
            "unchanged": len(rows) - inserted - updated,
            "db_only": db_only, "deleted": deleted}


def run_sync(url: str, rows: list[tuple], prune: bool) -> dict:
    import psycopg
    with psycopg.connect(url) as conn:  # commits on success, rolls back on error
        return sync(conn, rows, prune)


def describe_db_error(exc) -> str:
    diag = getattr(exc, "diag", None)
    return (f"database error: {type(exc).__name__} "
            f"sqlstate={getattr(exc, 'sqlstate', None)} "
            f"constraint={getattr(diag, 'constraint_name', None)}")


# ------------------------------------------------------------------ main


def main(argv=None, environ=None, run_sync=run_sync) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--prune", action="store_true",
                    help="delete DB rows whose sourceArticleId is not in the input")
    ap.add_argument("--allow-remote", action="store_true",
                    help="allow a DATABASE_URL host other than localhost")
    args = ap.parse_args(argv)

    url = environ.get("DATABASE_URL")
    problem = check_database_url(url, args.allow_remote, environ)
    if problem:
        print(f"config error: {problem}")
        return 2
    if not args.input.is_file():
        print(f"config error: input not found: {args.input}")
        return 2

    raw = args.input.read_bytes()
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        lines = None
    n = len(lines) if lines is not None else 0
    print(f"input {args.input} sha256={hashlib.sha256(raw).hexdigest()} "
          f"records={n} parserVersion={PARSER_VERSION}")
    if lines is None:
        print("validation failed: input is not UTF-8; database not touched")
        return 1

    records, errors = load_records(lines)
    if errors:
        for e in errors:
            print(e)
        print(f"validation failed: {len(errors)} errors; database not touched")
        return 1
    print(f"validated {len(records)}")

    import psycopg
    try:
        c = run_sync(url, [to_row(r) for r in records], args.prune)
    except psycopg.Error as exc:
        print(describe_db_error(exc))
        print("rolled back")
        return 3
    print(f"inserted {c['inserted']} updated {c['updated']} unchanged {c['unchanged']} "
          f"db_only {c['db_only']} deleted {c['deleted']}")
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
