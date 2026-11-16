"""Offline tests for scripts/convert_eval_v2.py. Synthetic CSVs in a temp folder.
Every text cell carries a sentinel, and every run asserts that no sentinel, no
kb_used_id and no path is printed."""
import contextlib
import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import convert_eval_v2 as conv  # noqa: E402
import run_eval  # noqa: E402

TEXT = "SENTINEL_TEXT_k3w9"
KB_ID = "777000777"
QUERY = f'  {TEXT} line one,\r\n"quoted" line two '
COLUMNS = ("row_id", "date", "channel", "type", "subject", "tech_title", "query",
           "kb_used_id", "kb_used_title", "kb_also_ok", "notes", "sample_pick")


def record(row_id, **over):
    """One synthetic row. Every cell except the IDs carries the sentinel."""
    values = {col: f"{TEXT} {col}" for col in COLUMNS}
    values.update(row_id=row_id, kb_used_id=KB_ID, tech_title="", kb_also_ok="",
                  query=QUERY)
    values.update(over)
    return values


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def convert(self, records, *args, out=None, columns=COLUMNS):
        csv_path = self.dir / "pilot.csv"
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
        out_path = self.dir / "cases.json" if out is None else out
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = conv.main([str(csv_path), "--out", str(out_path), *args])
        text = buf.getvalue()
        for secret in (TEXT, KB_ID, str(self.dir), str(ROOT)):
            self.assertNotIn(secret, text)
        return code, text.splitlines(), out_path, csv_path

    def assert_nothing_written(self, records, expected_line, *args, code=conv.EXIT_PROBLEMS):
        got, lines, out_path, _ = self.convert(records, *args)
        self.assertEqual(got, code, lines)
        self.assertIn(expected_line, lines)
        self.assertFalse(out_path.exists())
        return lines


class ConvertTests(Base):
    def test_converts_and_reports_row_ids_only(self):
        records = [record("p001"),
                   record("p002", tech_title=f"  {TEXT} title  "),
                   record("p003", kb_used_id="not a number", kb_also_ok=TEXT)]
        code, lines, out_path, csv_path = self.convert(records, "--exclude", "p003")
        self.assertEqual(code, 0, lines)
        payload = out_path.read_bytes()
        self.assertEqual(lines, [
            "convert-eval-v2-v1",
            f"input_sha256 {hashlib.sha256(csv_path.read_bytes()).hexdigest()}",
            "rows 3",
            "input_problems 0",
            "excluded_by_flag 1",
            "excluded p003",
            "exclude_not_in_file 0",
            "query_too_long 0",
            "cases 2",
            "technician_query_null 1",
            "technician_query_present 1",
            f"output_sha256 {hashlib.sha256(payload).hexdigest()}",
            "wrote 2 cases",
        ])
        cases = json.loads(payload.decode("utf-8"))
        self.assertEqual(cases, [
            {"id": "p001", "customer_query": QUERY, "technician_query": None,
             "target_article_ids": [KB_ID], "has_match": True},
            {"id": "p002", "customer_query": QUERY, "technician_query": f"{TEXT} title",
             "target_article_ids": [KB_ID], "has_match": True},
        ])
        self.assertEqual(run_eval.validate_cases(cases), [])

    def test_whitespace_only_tech_title_is_null(self):
        code, lines, out_path, _ = self.convert([record("p001", tech_title=" \t ")])
        self.assertEqual(code, 0, lines)
        self.assertIn("technician_query_null 1", lines)
        self.assertIsNone(json.loads(out_path.read_text(encoding="utf-8"))[0]
                          ["technician_query"])

    def test_too_long_query_is_listed_and_excluded_never_truncated(self):
        limit = run_eval.MAX_QUERY_CHARS
        records = [record("p001", query="x" * (limit + 1)),
                   record("p002", query="y" * limit)]
        code, lines, out_path, _ = self.convert(records)
        self.assertEqual(code, 0, lines)
        self.assertIn("query_too_long 1", lines)
        self.assertIn("too_long p001", lines)
        cases = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual([c["id"] for c in cases], ["p002"])
        self.assertEqual(cases[0]["customer_query"], "y" * limit)

    def test_comma_separated_and_repeated_exclude(self):
        records = [record("p001"), record("p002"), record("p003"), record("p004")]
        code, lines, out_path, _ = self.convert(records, "--exclude", "p001,p002",
                                                "--exclude", "p004")
        self.assertEqual(code, 0, lines)
        self.assertIn("excluded_by_flag 3", lines)
        self.assertEqual([c["id"] for c in json.loads(out_path.read_text(encoding="utf-8"))],
                         ["p003"])


class RefusalTests(Base):
    def test_out_inside_the_repo_is_refused(self):
        for out in (ROOT / "tests" / "fixtures" / "never_written.json",
                    ROOT / "scripts" / ".." / "never_written.json",
                    ROOT / "never_written.json"):
            with self.subTest(out=str(out.relative_to(ROOT))):
                code, lines, _, _ = self.convert([record("p001")], out=out)
                self.assertEqual(code, conv.EXIT_USAGE)
                self.assertEqual(lines, ["usage error: --out must be outside the repository"])
                self.assertFalse(out.exists())

    def test_existing_out_is_not_overwritten(self):
        out = self.dir / "cases.json"
        out.write_text("keep", encoding="utf-8")
        code, lines, _, _ = self.convert([record("p001")], out=out)
        self.assertEqual(code, conv.EXIT_USAGE)
        self.assertIn("refusing to overwrite", lines[0])
        self.assertEqual(out.read_text(encoding="utf-8"), "keep")

    def test_missing_out_directory(self):
        code, lines, _, _ = self.convert([record("p001")], out=self.dir / "nope" / "c.json")
        self.assertEqual(code, conv.EXIT_USAGE)
        self.assertIn("directory does not exist", lines[0])

    def test_bad_exclude_value(self):
        code, lines, out_path, _ = self.convert([record("p001")], "--exclude", "p 1")
        self.assertEqual(code, conv.EXIT_USAGE)
        self.assertFalse(out_path.exists())

    def test_missing_column(self):
        columns = tuple(c for c in COLUMNS if c != "tech_title")
        code, lines, out_path, _ = self.convert([record("p001")], columns=columns)
        self.assertEqual(code, conv.EXIT_USAGE)
        self.assertEqual(lines, ["input error: CSV header must have exactly one "
                                 "tech_title column"])
        self.assertFalse(out_path.exists())


class ProblemTests(Base):
    def test_input_problems_write_nothing(self):
        for name, bad, line in [
            ("duplicate", record("p001"), "duplicate_row_id p001"),
            ("bad id", record(f"{TEXT} id"), "bad_row_id record 2"),
            ("bad kb id", record("p002", kb_used_id=f"{TEXT}"), "bad_kb_used_id p002"),
            ("also ok", record("p002", kb_also_ok="123"), "kb_also_ok_not_empty p002"),
            ("blank query", record("p002", query="  \n "), "blank_query p002"),
            ("long title", record("p002", tech_title="t" * (run_eval.MAX_QUERY_CHARS + 1)),
             "tech_title_too_long p002"),
        ]:
            with self.subTest(problem=name):
                lines = self.assert_nothing_written([record("p001"), bad], line)
                self.assertIn("input_problems 1", lines)
                self.assertEqual(lines[-1], "nothing written")

    def test_exclude_not_in_file_writes_nothing(self):
        lines = self.assert_nothing_written([record("p001")], "not_in_file p999",
                                            "--exclude", "p999")
        self.assertIn("exclude_not_in_file 1", lines)

    def test_everything_excluded_writes_nothing(self):
        self.assert_nothing_written([record("p001")], "no cases to write",
                                    "--exclude", "p001")


if __name__ == "__main__":
    unittest.main()
