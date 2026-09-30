"""Offline tests for scripts/ingest_kb.py. No database, no real data: records are
built here from placeholder text and the DB layer is replaced by a fake."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ingest_kb  # noqa: E402
import psycopg  # noqa: E402

SENTINEL = "SENTINEL_VALUE_7q3x"
LOCAL_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


def record(sid="101", **over):
    rec = {
        "sourceArticleId": sid,
        "title": f"Placeholder title {sid}",
        "url": f"https://td.usd.edu/TDClient/33/Portal/KB/Article/{sid}/Placeholder",
        "category": "Placeholder category",
        "tags": ["alpha", "beta"],
        "description": "Placeholder description.",
        "publishedTime": "2020-01-02T03:04Z",
        "modifiedTime": None,
        "fullText": "Placeholder body text.",
        "question": None,
        "answer": None,
        "issue": "Placeholder issue.",
        "environment": None,
        "cause": None,
        "resolution": "Placeholder resolution.",
        "sections": [{"heading": None, "text": "Intro."},
                     {"heading": "Issue", "text": "Placeholder issue."}],
        "staffOnly": {"text": "Staff text.", "links": [{"text": "Link", "href": "https://x.test/"}]},
        "audienceLabels": [],
        "footer": None,
        "parserVersion": ingest_kb.PARSER_VERSION,
        "warnings": [],
    }
    rec.update(over)
    return rec


class FakeSync:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result or {"inserted": 0, "updated": 0, "unchanged": 0,
                                 "db_only": 0, "deleted": 0}
        self.exc = exc

    def __call__(self, url, rows, prune):
        self.calls.append((url, rows, prune))
        if self.exc:
            raise self.exc
        return self.result


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, lines):
        p = Path(self.tmp.name) / "articles.jsonl"
        p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
        return p

    def run_main(self, lines, args=(), env=None, fake=None):
        path = self.write([l if isinstance(l, str) else json.dumps(l) for l in lines])
        env = {"DATABASE_URL": LOCAL_URL} if env is None else env
        fake = fake or FakeSync()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = ingest_kb.main(["--input", str(path), *args], environ=env, run_sync=fake)
            except SystemExit as e:  # argparse
                code = e.code
        return code, out.getvalue(), fake


class ValidationTests(Base):
    def test_valid_record_passes(self):
        self.assertEqual(ingest_kb.validate_record(record()), [])

    def assert_invalid(self, rec_or_line, expect_label, expect_rule):
        code, out, fake = self.run_main([record("5"), rec_or_line])
        self.assertEqual(code, 1, out)
        self.assertEqual(fake.calls, [], "must not reach the database")
        self.assertIn(f"invalid record {expect_label}: ", out)
        self.assertIn(expect_rule, out)
        self.assertIn("database not touched", out)
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn("Placeholder", out)

    def test_missing_key(self):
        rec = record("77", title=SENTINEL)
        del rec["footer"]
        self.assert_invalid(rec, "sourceArticleId=77", "missing key footer")

    def test_extra_key(self):
        self.assert_invalid(record("77", extra=SENTINEL), "sourceArticleId=77",
                            "unexpected key extra")

    def test_wrong_type(self):
        self.assert_invalid(record("77", tags=["ok", 5], title=SENTINEL),
                            "sourceArticleId=77", "tags must be list[str]")

    def test_bad_id_uses_line_number(self):
        self.assert_invalid(record("0077"), "line 2", "sourceArticleId must be")
        self.assert_invalid(record(SENTINEL), "line 2", "sourceArticleId must be")

    def test_duplicate_id(self):
        self.assert_invalid(record("5"), "sourceArticleId=5", "duplicate sourceArticleId")

    def test_bad_url(self):
        self.assert_invalid(record("77", url=SENTINEL), "sourceArticleId=77", "url must be")

    def test_url_id_mismatch(self):
        url = "https://td.usd.edu/TDClient/33/Portal/KB/Article/78/Placeholder"
        self.assert_invalid(record("77", url=url), "sourceArticleId=77",
                            "url article ID must equal sourceArticleId")

    def test_empty_title(self):
        self.assert_invalid(record("77", title="   "), "sourceArticleId=77",
                            "title must be a non-empty string")

    def test_bad_time(self):
        self.assert_invalid(record("77", publishedTime=SENTINEL), "sourceArticleId=77",
                            "publishedTime must be null or")

    def test_wrong_parser_version(self):
        self.assert_invalid(record("77", parserVersion="1"), "sourceArticleId=77",
                            "parserVersion must be")

    def test_bad_sections(self):
        self.assert_invalid(record("77", sections=[{"heading": 1, "text": SENTINEL}]),
                            "sourceArticleId=77", "sections heading must be str or null")

    def test_bad_staff_only(self):
        self.assert_invalid(record("77", staffOnly={"text": SENTINEL}),
                            "sourceArticleId=77", "staffOnly must be null or {text, links}")

    def test_nul_character(self):
        self.assert_invalid(record("77", fullText=f"a\x00{SENTINEL}"),
                            "sourceArticleId=77", "contains a NUL character")

    def test_bad_json_line(self):
        self.assert_invalid('{"sourceArticleId": "77", ' + SENTINEL, "line 2",
                            "not valid JSON")

    def test_not_an_object(self):
        self.assert_invalid(json.dumps([SENTINEL]), "line 2", "not a JSON object")

    def test_empty_file(self):
        code, out, fake = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("input has no records", out)
        self.assertEqual(fake.calls, [])

    def test_all_errors_collected(self):
        code, out, _ = self.run_main([record("1", title=""), record("2", tags=None),
                                      record("3")])
        self.assertEqual(code, 1)
        self.assertIn("invalid record sourceArticleId=1: title", out)
        self.assertIn("invalid record sourceArticleId=2: tags", out)
        self.assertIn("validation failed: 2 errors", out)


class MappingTests(Base):
    def test_rows_sorted_numerically_and_mapped_in_column_order(self):
        code, out, fake = self.run_main([record("10"), record("9"), record("100")])
        self.assertEqual(code, 0, out)
        rows = fake.calls[0][1]
        self.assertEqual([r[0] for r in rows], ["9", "10", "100"])
        self.assertEqual(len(rows[0]), len(ingest_kb.COLUMNS))
        self.assertEqual(rows[0], tuple(record("9")[k] for k in ingest_kb.COLUMNS))

    def test_mapping_is_deterministic(self):
        rec = record("42")
        self.assertEqual(ingest_kb.to_row(copy.deepcopy(rec)), ingest_kb.to_row(rec))

    def test_columns_cover_exactly_the_record_keys(self):
        self.assertEqual(set(ingest_kb.COLUMNS), set(record()))


class ConfigTests(Base):
    def test_missing_database_url(self):
        code, out, fake = self.run_main([record()], env={})
        self.assertEqual(code, 2)
        self.assertIn("DATABASE_URL is not set", out)
        self.assertEqual(fake.calls, [])

    def test_remote_host_refused_without_flag(self):
        env = {"DATABASE_URL": f"postgresql://u:{SENTINEL}@db.example.com:5432/postgres"}
        code, out, fake = self.run_main([record()], env=env)
        self.assertEqual(code, 2)
        self.assertIn("--allow-remote", out)
        self.assertNotIn(SENTINEL, out)
        self.assertEqual(fake.calls, [])

    def test_remote_host_allowed_with_flag(self):
        env = {"DATABASE_URL": "postgresql://u:p@db.example.com:5432/postgres"}
        code, out, fake = self.run_main([record()], args=["--allow-remote"], env=env)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(fake.calls), 1)

    def test_local_hosts_accepted(self):
        for url in (LOCAL_URL, "postgresql://postgres@localhost/postgres",
                    "host=::1 dbname=postgres", "postgresql:///postgres"):
            self.assertIsNone(ingest_kb.check_database_url(url, False, {}), url)

    def test_pghost_fallback_is_checked(self):
        self.assertIsNotNone(ingest_kb.check_database_url(
            "postgresql:///postgres", False, {"PGHOST": "db.example.com"}))

    def test_mixed_hosts_refused(self):
        self.assertIsNotNone(ingest_kb.check_database_url(
            "host=localhost,db.example.com dbname=postgres", False, {}))

    def test_invalid_url(self):
        code, out, _ = self.run_main([record()], env={"DATABASE_URL": f"={SENTINEL}"})
        self.assertEqual(code, 2)
        self.assertNotIn(SENTINEL, out)

    def test_missing_input(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ingest_kb.main(["--input", str(Path(self.tmp.name) / "nope.jsonl")],
                                  environ={"DATABASE_URL": LOCAL_URL}, run_sync=FakeSync())
        self.assertEqual(code, 2)

    def test_unknown_argument_is_usage_error(self):
        code, _, fake = self.run_main([record()], args=["--bogus"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])


class OutputTests(Base):
    def test_success_output(self):
        fake = FakeSync({"inserted": 1, "updated": 2, "unchanged": 3,
                         "db_only": 4, "deleted": 0})
        recs = [record(str(i), fullText=f"{SENTINEL} body") for i in range(1, 7)]
        code, out, fake = self.run_main(recs, fake=fake)
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^input .+ sha256=[0-9a-f]{64} records=6 parserVersion=2$")
        self.assertEqual(lines[1:], ["validated 6",
                                     "inserted 1 updated 2 unchanged 3 db_only 4 deleted 0",
                                     "ok"])
        self.assertNotIn(SENTINEL, out)
        self.assertIs(fake.calls[0][2], False)

    def test_prune_is_opt_in(self):
        _, _, fake = self.run_main([record()])
        self.assertIs(fake.calls[0][2], False)
        _, _, fake = self.run_main([record()], args=["--prune"])
        self.assertIs(fake.calls[0][2], True)

    def test_database_error_prints_only_class_sqlstate_constraint(self):
        exc = psycopg.errors.CheckViolation(f"new row violates ... DETAIL: Failing row {SENTINEL}")
        code, out, _ = self.run_main([record()], fake=FakeSync(exc=exc))
        self.assertEqual(code, 3)
        self.assertIn("database error: CheckViolation sqlstate=23514 constraint=None", out)
        self.assertIn("rolled back", out)
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn("DETAIL", out)

    def test_connection_error_is_exit_3_without_message(self):
        exc = psycopg.OperationalError(f"connection failed: password={SENTINEL}")
        code, out, _ = self.run_main([record()], fake=FakeSync(exc=exc))
        self.assertEqual(code, 3)
        self.assertIn("database error: OperationalError sqlstate=None", out)
        self.assertNotIn(SENTINEL, out)

    def test_describe_db_error_uses_constraint_name(self):
        class Diag:
            constraint_name = "articles_url_check"

        class Fake(Exception):
            sqlstate = "23514"
            diag = Diag()

        self.assertEqual(ingest_kb.describe_db_error(Fake(SENTINEL)),
                         "database error: Fake sqlstate=23514 constraint=articles_url_check")


if __name__ == "__main__":
    unittest.main()
