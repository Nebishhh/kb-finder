"""Tests for scripts/check_eval_ids.py. Offline: synthetic CSVs in a temp folder and a
fake connection. Every run asserts that no placeholder cell text, password or path is
printed. One live test is skipped unless KB_FINDER_TEST_DATABASE_URL is set."""
import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import check_eval_ids  # noqa: E402
import embed_kb  # noqa: E402
import psycopg  # noqa: E402

LIVE_URL = os.environ.get("KB_FINDER_TEST_DATABASE_URL")
PASSWORD = "SENTINEL_PASSWORD_q4m1"
LOCAL_URL = f"postgresql://postgres:{PASSWORD}@127.0.0.1:54322/postgres"
REMOTE_URL = f"postgresql://kb_app.tenant:{PASSWORD}@db.example.test:6543/postgres"
CFG_SHA = embed_kb.load_config(embed_kb.CONFIG_PATH)[1]
TEXT = "SENTINEL_TEXT_z8"
COLUMNS = ("row_id", "date", "channel", "type", "subject", "tech_title", "query",
           "kb_used_id", "kb_used_title", "kb_also_ok", "notes", "sample_pick")


def record(row_id, kb_used_id, **over):
    """One synthetic row. Every cell except the two IDs carries the sentinel."""
    values = {col: f"{TEXT} {col}" for col in COLUMNS}
    values.update(row_id=row_id, kb_used_id=kb_used_id,
                  query=f'{TEXT} line one,\n"quoted" line two')
    values.update(over)
    return values


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        db = self.conn.db
        self.conn.statements.append((query, params))
        q = query.strip().lower()
        if db.get("fail_on") and db["fail_on"] in q:
            raise psycopg.errors.QueryCanceled("canceled")
        if q.startswith("select count(*)"):
            self.rows = [(db["total"],)]
        elif q.startswith("select a.source_article_id"):
            cfg, ids = params
            self.rows = [(i, db["articles"][i] and cfg == CFG_SHA)
                         for i in ids if i in db["articles"]]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.statements = []
        self.rolled_back = self.committed = self.closed = 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back += 1

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed += 1


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, records, columns=COLUMNS, encoding="utf-8"):
        path = self.dir / "cases.csv"
        with open(path, "w", encoding=encoding, newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
        return path

    def write_raw(self, data: bytes):
        path = self.dir / "cases.csv"
        path.write_bytes(data)
        return path

    def run_main(self, path, db=None, url=LOCAL_URL):
        """Return (exit code, output lines, fake connections opened)."""
        if db is None:
            db = {"total": 3, "articles": {"101": True, "102": True, "103": False}}
        conns = []

        def connect(_url, **kwargs):
            self.assertEqual(kwargs, {"prepare_threshold": None})
            conns.append(FakeConn(db))
            return conns[-1]

        def lookup(u, sha, ids):
            return check_eval_ids.lookup(u, sha, ids, connect=connect)

        out = io.StringIO()
        environ = {} if url is None else {"DATABASE_URL": url}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check_eval_ids.main([str(path)], environ=environ, lookup=lookup)
        text = out.getvalue()
        for secret in (TEXT, PASSWORD, str(self.dir)):
            self.assertNotIn(secret, text)
        return code, text.splitlines(), conns


class ReportTests(Base):
    def test_all_present(self):
        code, lines, _ = self.run_main(self.write([record("r1", "101"), record("r2", "102")]))
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            "check-eval-ids-v1",
            "rows 2",
            "rows_checked 2",
            "distinct_kb_used_id 2",
            "corpus_articles 3",
            f"embedding_config {CFG_SHA}",
            "input_problems 0",
            "missing_from_corpus 0",
            "no_embedding 0",
        ])

    def test_missing_prints_row_id_and_not_the_article_id(self):
        code, lines, _ = self.run_main(
            self.write([record("r1", "101"), record("r2", "424242"), record("r3", "102")]))
        self.assertEqual(code, 1)
        self.assertIn("missing_from_corpus 1", lines)
        self.assertIn("missing r2", lines)
        self.assertNotIn("424242", "\n".join(lines))

    def test_two_rows_with_one_missing_article_are_both_listed(self):
        code, lines, _ = self.run_main(
            self.write([record("r1", "777"), record("r2", "777"), record("r3", "101")]))
        self.assertEqual(code, 1)
        self.assertIn("distinct_kb_used_id 2", lines)
        self.assertIn("missing_from_corpus 2", lines)
        self.assertLess(lines.index("missing r1"), lines.index("missing r2"))

    def test_article_without_embedding(self):
        code, lines, _ = self.run_main(self.write([record("r1", "101"), record("r2", "103")]))
        self.assertEqual(code, 1)
        self.assertIn("missing_from_corpus 0", lines)
        self.assertIn("no_embedding 1", lines)
        self.assertIn("no_embedding_for r2", lines)

    def test_ids_are_stripped(self):
        code, _, _ = self.run_main(self.write([record(" r1 ", " 101 ")]))
        self.assertEqual(code, 0)


class InputTests(Base):
    def test_quoted_multiline_cells_bom_and_any_column_order(self):
        columns = tuple(reversed(COLUMNS))
        path = self.write([record("r1", "101"), record("r2", "102")],
                          columns=columns, encoding="utf-8-sig")
        code, lines, _ = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertIn("rows 2", lines)

    def test_blank_lines_are_not_rows(self):
        path = self.write_raw(b"row_id,kb_used_id\r\n\r\nr1,101\r\n\r\n")
        code, lines, _ = self.run_main(path)
        self.assertEqual(code, 0)
        self.assertIn("rows 1", lines)

    def test_bad_kb_used_id_is_listed_and_never_sent_or_printed(self):
        rows = [record("r1", f"{TEXT} not an id"), record("r2", "101.0"),
                record("r3", ""), record("r4", "0101"), record("r5", "101")]
        code, lines, conns = self.run_main(self.write(rows))
        self.assertEqual(code, 1)
        self.assertIn("input_problems 4", lines)
        for rid in ("r1", "r2", "r3", "r4"):
            self.assertIn(f"bad_kb_used_id {rid}", lines)
        self.assertIn("rows_checked 1", lines)
        sent = [p for q, p in conns[0].statements if p]
        self.assertEqual(sent, [[CFG_SHA, ["101"]]])

    def test_bad_row_id_is_named_by_record_number(self):
        rows = [record("r1", "101"), record(f"{TEXT} oops", "102"), record("", "102")]
        code, lines, _ = self.run_main(self.write(rows))
        self.assertEqual(code, 1)
        self.assertIn("bad_row_id record 2", lines)
        self.assertIn("bad_row_id record 3", lines)

    def test_duplicate_row_id(self):
        code, lines, _ = self.run_main(self.write([record("r1", "101"), record("r1", "102")]))
        self.assertEqual(code, 1)
        self.assertIn("duplicate_row_id r1", lines)
        self.assertIn("rows_checked 1", lines)

    def test_wrong_column_count(self):
        path = self.write_raw(
            f"row_id,query,kb_used_id\nr1,{TEXT}\nr2,{TEXT},101,{TEXT}\nr3,{TEXT},101\n"
            .encode())
        code, lines, _ = self.run_main(path)
        self.assertEqual(code, 1)
        self.assertIn("bad_column_count record 1", lines)
        self.assertIn("bad_column_count record 2", lines)
        self.assertIn("rows_checked 1", lines)

    def test_missing_or_repeated_column_is_refused_without_the_header(self):
        cases = {
            "row_id": f"{TEXT},kb_used_id\nr1,101\n",
            "kb_used_id": f"row_id,{TEXT}\nr1,101\n",
        }
        for col, data in cases.items():
            code, lines, conns = self.run_main(self.write_raw(data.encode()))
            self.assertEqual(code, 2)
            self.assertEqual(
                lines, [f"input error: CSV header must have exactly one {col} column"])
            self.assertEqual(conns, [])
        code, lines, _ = self.run_main(
            self.write_raw(b"row_id,kb_used_id,row_id\nr1,101,r1\n"))
        self.assertEqual(code, 2)

    def test_empty_file(self):
        code, lines, _ = self.run_main(self.write_raw(b""))
        self.assertEqual((code, lines), (2, ["input error: CSV is empty"]))

    def test_file_not_found_prints_class_only(self):
        code, lines, conns = self.run_main(self.dir / f"{TEXT}.csv")
        self.assertEqual((code, lines), (2, ["input error: cannot read CSV: FileNotFoundError"]))
        self.assertEqual(conns, [])

    def test_bad_quoting_prints_class_and_line_only(self):
        path = self.write_raw(f'row_id,query,kb_used_id\nr1,"{TEXT} a"b,101\n'.encode())
        code, lines, conns = self.run_main(path)
        self.assertEqual(code, 2)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("input error: cannot parse CSV: Error near line "))
        self.assertEqual(conns, [])

    def test_not_utf8_prints_class_only(self):
        path = self.write_raw(b"row_id,kb_used_id\nr1,101\n\xff\xfe,101\n")
        code, lines, _ = self.run_main(path)
        self.assertEqual(code, 2)
        self.assertTrue(
            lines[0].startswith("input error: cannot parse CSV: UnicodeDecodeError"))


class DatabaseTests(Base):
    def test_read_only_and_always_rolled_back(self):
        _, _, conns = self.run_main(self.write([record("r1", "101")]))
        conn = conns[0]
        self.assertEqual(conn.statements[0], ("set transaction read only", None))
        self.assertEqual((conn.rolled_back, conn.committed, conn.closed), (1, 0, 1))
        for query, _ in conn.statements[1:]:
            self.assertTrue(query.lower().startswith("select "))

    def test_only_distinct_sorted_digit_ids_are_sent(self):
        rows = [record("r1", "102"), record("r2", "101"), record("r3", "102")]
        _, _, conns = self.run_main(self.write(rows))
        sent = [p for q, p in conns[0].statements if p]
        self.assertEqual(sent, [[CFG_SHA, ["101", "102"]]])

    def test_database_error(self):
        db = {"total": 3, "articles": {}, "fail_on": "select a.source_article_id"}
        code, lines, conns = self.run_main(self.write([record("r1", "101")]), db=db)
        self.assertEqual(code, 3)
        self.assertEqual(
            lines, ["database error: QueryCanceled sqlstate=57014 constraint=None",
                    "rolled back"])
        self.assertEqual((conns[0].rolled_back, conns[0].closed), (1, 1))

    def test_url_unset_or_remote_is_refused_before_anything_is_read(self):
        path = self.dir / "does-not-exist.csv"
        code, lines, conns = self.run_main(path, url=None)
        self.assertEqual((code, lines), (2, ["config error: DATABASE_URL is not set"]))
        code, lines, conns = self.run_main(path, url=REMOTE_URL)
        self.assertEqual(
            (code, lines), (2, ["config error: DATABASE_URL host is not local"]))
        self.assertEqual(conns, [])

    def test_no_allow_remote_flag(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            check_eval_ids.main(["x.csv", "--allow-remote"], environ={})
        self.assertEqual(ctx.exception.code, 2)


class CliTests(unittest.TestCase):
    def test_unexpected_error_prints_class_only(self):
        def boom():
            raise RuntimeError(TEXT)

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = check_eval_ids.cli(main=boom)
        self.assertEqual(code, 5)
        self.assertEqual(out.getvalue(), "unexpected error: RuntimeError\n")


@unittest.skipUnless(LIVE_URL, "KB_FINDER_TEST_DATABASE_URL not set")
class LiveTests(unittest.TestCase):
    def test_unknown_id_is_missing(self):
        total, found = check_eval_ids.lookup(LIVE_URL, CFG_SHA, ["999999999999999999"])
        self.assertGreaterEqual(total, 0)
        self.assertEqual(found, {})


if __name__ == "__main__":
    unittest.main()
