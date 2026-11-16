#!/usr/bin/env python
"""Eval v2: convert the pilot CSV into a run_eval.py cases file.

Input : a CSV path (argv) with a header row. Only row_id, query, tech_title,
        kb_used_id and kb_also_ok are read.
Output: --out PATH, a new JSON cases file (schema in SPEC.md, Evaluation). It
        holds query text, so --out must be outside this repository.

Run:  .venv/Scripts/python scripts/convert_eval_v2.py CSV_PATH --out PATH
          [--exclude ROW_ID ...]

Rules
-----
Mapping      id = row_id. customer_query = the query cell, unchanged.
             technician_query = the tech_title cell with outer whitespace
             removed, or null when it is blank. Nothing is invented.
             target_article_ids = [kb_used_id]. has_match = true.
Exclusion    --exclude names rows to leave out (repeat it or use commas), for
             rows whose article is not in the corpus. A named row that is not in
             the file is an error.
Too long     A row whose query is longer than run_eval.MAX_QUERY_CHARS is listed
             and left out. It is never truncated.
Input        A bad column count, a bad or duplicate row_id, a bad kb_used_id, a
problems     non-empty kb_also_ok, a blank query or an over-long tech_title.
             Any of them: nothing is written.
Check        The cases are passed through run_eval.validate_cases before writing.
Output       Deterministic lines: counts, the two sha256 values and row_ids.
             Never a query, a title, a kb_used_id, the header or a path. Errors
             print the exception class only. --out is never overwritten.
Exit codes   0 written, 1 input problem (nothing written), 2 usage or
             unreadable input, 5 unexpected error.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path

import run_eval

FORMAT_VERSION = "convert-eval-v2-v1"
REPO_ROOT = Path(__file__).resolve().parent.parent
NEEDED = ("row_id", "tech_title", "query", "kb_used_id", "kb_also_ok")
# Same patterns as check_eval_ids.py. Row IDs are printed and become case IDs.
ROW_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
ARTICLE_ID_RE = re.compile(r"[1-9][0-9]{0,17}")
FIELD_LIMIT = 1_000_000

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_UNEXPECTED = 0, 1, 2, 5


class InputError(Exception):
    """A failure with a message that is safe to print (no cell text, no path)."""


def check_out(out: Path) -> str | None:
    if out.resolve().is_relative_to(REPO_ROOT):
        return "--out must be outside the repository"
    if out.exists():
        return "--out already exists, refusing to overwrite"
    if not out.parent.is_dir():
        return "--out directory does not exist"
    return None


def parse_exclude(items: list[str]) -> set[str]:
    exclude = set()
    for item in items:
        for rid in item.split(","):
            rid = rid.strip()
            if not ROW_ID_RE.fullmatch(rid):
                raise InputError("--exclude takes row_ids: 1-64 letters, digits, "
                                 "'_', '.' or '-'")
            exclude.add(rid)
    return exclude


def read_rows(raw: bytes, exclude: set[str]):
    """Return (rows, excluded, problems, records). rows is (row_id, query,
    technician query or None, kb_used_id) in file order; excluded is row_ids;
    problems is (kind, row_id or "record N")."""
    csv.field_size_limit(FIELD_LIMIT)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InputError("CSV is not UTF-8") from None
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows, excluded, problems, seen, records = [], [], [], set(), 0
    try:
        header = next(reader, None)
        if header is None:
            raise InputError("CSV is empty")
        names = [name.strip() for name in header]
        for col in NEEDED:
            if names.count(col) != 1:
                raise InputError(f"CSV header must have exactly one {col} column")
        at = {col: names.index(col) for col in NEEDED}
        width = len(names)
        for row in reader:
            if not row:
                continue
            records += 1
            if len(row) != width:
                problems.append(("bad_column_count", f"record {records}"))
                continue
            rid = row[at["row_id"]].strip()
            if not ROW_ID_RE.fullmatch(rid):
                problems.append(("bad_row_id", f"record {records}"))
                continue
            if rid in seen:
                problems.append(("duplicate_row_id", rid))
                continue
            seen.add(rid)
            if rid in exclude:
                excluded.append(rid)
                continue
            kid = row[at["kb_used_id"]].strip()
            query = row[at["query"]]
            title = row[at["tech_title"]].strip()
            if not ARTICLE_ID_RE.fullmatch(kid):
                problems.append(("bad_kb_used_id", rid))
            elif row[at["kb_also_ok"]].strip():
                problems.append(("kb_also_ok_not_empty", rid))
            elif not query.strip():
                problems.append(("blank_query", rid))
            elif len(title) > run_eval.MAX_QUERY_CHARS:
                problems.append(("tech_title_too_long", rid))
            else:
                rows.append((rid, query, title or None, kid))
    except csv.Error as exc:
        raise InputError(f"cannot parse CSV: {type(exc).__name__} near line "
                         f"{reader.line_num}") from None
    return rows, excluded, problems, records


def build_cases(rows) -> tuple[list[dict], list[str]]:
    cases, too_long = [], []
    for rid, query, title, kid in rows:
        if len(query) > run_eval.MAX_QUERY_CHARS:
            too_long.append(rid)
            continue
        cases.append({"id": rid, "customer_query": query, "technician_query": title,
                      "target_article_ids": [kid], "has_match": True})
    return cases, too_long


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv_path", type=Path, help="the eval v2 CSV")
    ap.add_argument("--out", required=True, type=Path,
                    help="new cases JSON file, outside the repository")
    ap.add_argument("--exclude", action="append", default=[], metavar="ROW_ID",
                    help="row_id to leave out; repeat or separate with commas")
    args = ap.parse_args(argv)

    problem = check_out(args.out)
    if problem:
        print(f"usage error: {problem}")
        return EXIT_USAGE
    try:
        exclude = parse_exclude(args.exclude)
    except InputError as exc:
        print(f"usage error: {exc}")
        return EXIT_USAGE
    try:
        raw = args.csv_path.read_bytes()
    except OSError as exc:
        print(f"input error: cannot read CSV: {type(exc).__name__}")
        return EXIT_USAGE
    try:
        rows, excluded, problems, records = read_rows(raw, exclude)
    except InputError as exc:
        print(f"input error: {exc}")
        return EXIT_USAGE

    unknown = sorted(exclude - set(excluded))
    cases, too_long = build_cases(rows)
    nulls = sum(1 for c in cases if c["technician_query"] is None)
    print(FORMAT_VERSION)
    print(f"input_sha256 {hashlib.sha256(raw).hexdigest()}")
    print(f"rows {records}")
    print(f"input_problems {len(problems)}")
    for kind, ref in problems:
        print(f"{kind} {ref}")
    print(f"excluded_by_flag {len(excluded)}")
    for rid in excluded:
        print(f"excluded {rid}")
    print(f"exclude_not_in_file {len(unknown)}")
    for rid in unknown:
        print(f"not_in_file {rid}")
    print(f"query_too_long {len(too_long)}")
    for rid in too_long:
        print(f"too_long {rid}")
    print(f"cases {len(cases)}")
    print(f"technician_query_null {nulls}")
    print(f"technician_query_present {len(cases) - nulls}")

    errors = run_eval.validate_cases(cases) if cases else ["no cases to write"]
    if problems or unknown or errors:
        for e in errors:
            print(e)
        print("nothing written")
        return EXIT_PROBLEMS

    payload = (json.dumps(cases, indent=2) + "\n").encode("utf-8")
    with open(args.out, "xb") as f:
        f.write(payload)
    print(f"output_sha256 {hashlib.sha256(payload).hexdigest()}")
    print(f"wrote {len(cases)} cases")
    return EXIT_OK


def cli(main=main) -> int:
    """main() with every unexpected error reduced to its class name: no message
    and no traceback, because either could carry cell text."""
    try:
        return main()
    except Exception as exc:
        print(f"unexpected error: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(cli())
