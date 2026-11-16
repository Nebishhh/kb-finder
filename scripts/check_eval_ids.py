#!/usr/bin/env python
"""Eval v2: report which kb_used_id values in a cases CSV are missing from the local corpus.

Input : a CSV path (argv) with a header row. Only the row_id and kb_used_id cells are
        kept; every other cell is dropped as its row is parsed and is never stored,
        compared or printed.
DB    : DATABASE_URL environment variable only (never argv). Local hosts only; this
        script has no --allow-remote.
Config: config/embedding/article-v1.json, read with embed_kb.load_config

Run:  .venv/Scripts/python scripts/check_eval_ids.py CSV_PATH

Rules
-----
Read only    One transaction that starts with SET TRANSACTION READ ONLY and is always
             rolled back. psycopg connects with prepare_threshold=None. Only digit
             strings are sent to the database.
Checks       missing       kb_used_id is not a source_article_id in articles
             no_embedding  the article exists but has no embedding for the config
                           file's config_sha256 (semantic and hybrid cannot return it)
Input        A row is checked only if it has the header's number of cells, a row_id
problems     matching ROW_ID_RE that has not been seen before, and a kb_used_id
             matching ARTICLE_ID_RE. Anything else is listed and not checked.
Output       Deterministic lines: counts, the config hash and row_ids. A row_id is
             printed only after it matches ROW_ID_RE; a row without one is named by its
             record number (1 = first non-blank row after the header). Never a
             kb_used_id, any other cell, the header, the CSV path or the connection
             string. Errors print the exception class only, never its message or a
             traceback.
Exit codes   0 every row checked and found, 1 something missing or an input problem,
             2 usage/config/unreadable input, 3 database error, 5 unexpected error.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

import embed_kb
import ingest_kb

FORMAT_VERSION = "check-eval-ids-v1"
NEEDED = ("row_id", "kb_used_id")
# Row IDs are printed, so only plain identifiers are accepted (as run_eval.CASE_ID_RE).
ROW_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# articles.source_article_id is checked against ^[1-9][0-9]*$ in the database.
ARTICLE_ID_RE = re.compile(r"[1-9][0-9]{0,17}")
FIELD_LIMIT = 1_000_000

EXIT_OK, EXIT_FOUND, EXIT_USAGE, EXIT_EXTERNAL, EXIT_UNEXPECTED = 0, 1, 2, 3, 5

COUNT_SQL = "select count(*) from articles"
LOOKUP_SQL = (
    "select a.source_article_id, e.source_article_id is not null "
    "from articles a left join article_embeddings e "
    "on e.source_article_id = a.source_article_id and e.config_sha256 = %s "
    "where a.source_article_id = any(%s)")


class InputError(Exception):
    """A failure with a message that is safe to print (no cell text, no path)."""


# ------------------------------------------------------------------ input


def read_pairs(path) -> tuple[list[tuple[str, str]], list[tuple[str, str]], int]:
    """Return (pairs, problems, records). pairs is (row_id, kb_used_id) per checked row
    in file order; problems is (kind, row_id or "record N"). Nothing else leaves here."""
    csv.field_size_limit(FIELD_LIMIT)
    pairs, problems, seen, records = [], [], set(), 0
    reader = None
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh, strict=True)
            header = next(reader, None)
            if header is None:
                raise InputError("CSV is empty")
            names = [name.strip() for name in header]
            width = len(names)
            for col in NEEDED:
                if names.count(col) != 1:
                    raise InputError(f"CSV header must have exactly one {col} column")
            rid_at, kid_at = names.index("row_id"), names.index("kb_used_id")
            del header, names
            for row in reader:
                if not row:
                    continue
                records += 1
                if len(row) != width:
                    problems.append(("bad_column_count", f"record {records}"))
                    continue
                rid, kid = row[rid_at].strip(), row[kid_at].strip()
                del row
                if not ROW_ID_RE.fullmatch(rid):
                    problems.append(("bad_row_id", f"record {records}"))
                elif rid in seen:
                    problems.append(("duplicate_row_id", rid))
                elif not ARTICLE_ID_RE.fullmatch(kid):
                    seen.add(rid)
                    problems.append(("bad_kb_used_id", rid))
                else:
                    seen.add(rid)
                    pairs.append((rid, kid))
    except InputError:
        raise
    except OSError as exc:
        raise InputError(f"cannot read CSV: {type(exc).__name__}") from None
    except (UnicodeError, csv.Error) as exc:
        line = reader.line_num if reader is not None else 0
        raise InputError(
            f"cannot parse CSV: {type(exc).__name__} near line {line}") from None
    return pairs, problems, records


# ------------------------------------------------------------------ database


def lookup(url: str, config_sha256: str, ids: list[str], connect=None):
    """Return (articles in the corpus, {source_article_id: has an embedding}) for ids."""
    if connect is None:
        import psycopg
        connect = psycopg.connect
    # Not "with connect(...)": that commits on success. This always rolls back.
    conn = connect(url, prepare_threshold=None)
    try:
        with conn.cursor() as cur:
            cur.execute("set transaction read only")
            cur.execute(COUNT_SQL)
            total = cur.fetchone()[0]
            cur.execute(LOOKUP_SQL, [config_sha256, ids])
            found = {sid: bool(embedded) for sid, embedded in cur.fetchall()}
        return total, found
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


# ------------------------------------------------------------------ report


def render(pairs, problems, records, total, found, config_sha256) -> tuple[list[str], bool]:
    """Return (lines, clean). clean is False if anything is missing or was not checked."""
    missing = [rid for rid, kid in pairs if kid not in found]
    no_embedding = [rid for rid, kid in pairs if found.get(kid) is False]
    lines = [
        FORMAT_VERSION,
        f"rows {records}",
        f"rows_checked {len(pairs)}",
        f"distinct_kb_used_id {len({kid for _, kid in pairs})}",
        f"corpus_articles {total}",
        f"embedding_config {config_sha256}",
        f"input_problems {len(problems)}",
    ]
    lines += [f"{kind} {ref}" for kind, ref in problems]
    lines.append(f"missing_from_corpus {len(missing)}")
    lines += [f"missing {rid}" for rid in missing]
    lines.append(f"no_embedding {len(no_embedding)}")
    lines += [f"no_embedding_for {rid}" for rid in no_embedding]
    return lines, not (problems or missing or no_embedding)


# ------------------------------------------------------------------ main


def main(argv=None, environ=None, lookup=lookup) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv_path", type=Path,
                    help="CSV with row_id and kb_used_id columns")
    args = ap.parse_args(argv)

    url = environ.get("DATABASE_URL")
    problem = ingest_kb.check_database_url(url, False, environ)
    if problem:
        problem = problem.replace("; pass --allow-remote to use it", "")
        print(f"config error: {problem}")
        return EXIT_USAGE
    try:
        _, config_sha256 = embed_kb.load_config(embed_kb.CONFIG_PATH)
    except embed_kb.EmbedError as exc:
        print(f"config error: {exc}")
        return EXIT_USAGE
    try:
        pairs, problems, records = read_pairs(args.csv_path)
    except InputError as exc:
        print(f"input error: {exc}")
        return EXIT_USAGE

    import psycopg
    try:
        total, found = lookup(url, config_sha256, sorted({kid for _, kid in pairs}))
    except psycopg.Error as exc:
        print(ingest_kb.describe_db_error(exc))
        print("rolled back")
        return EXIT_EXTERNAL
    lines, clean = render(pairs, problems, records, total, found, config_sha256)
    for line in lines:
        print(line)
    return EXIT_OK if clean else EXIT_FOUND


def cli(main=main) -> int:
    """main() with every unexpected error reduced to its class name: no message and no
    traceback, because either could carry cell text."""
    try:
        return main()
    except Exception as exc:
        print(f"unexpected error: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(cli())
