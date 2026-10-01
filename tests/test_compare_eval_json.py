"""Offline tests for scripts/compare_eval_json.py. Reports are synthetic and
built with run_eval.json_report, so the tests follow the real --json-out
format. No real eval results are read."""
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
import compare_eval_json as cmp  # noqa: E402
import run_eval  # noqa: E402


def results(*pairs):
    return [{"rank": i, "sourceArticleId": sid, "title": f"Title {sid}", "url": "u",
             "score": score} for i, (sid, score) in enumerate(pairs, start=1)]


def report(mode="lexical", cases_sha256="a" * 64):
    tracks = {}
    for track, field in run_eval.TRACKS:
        positives = [
            {"case_id": "case_001", "best_rank": 1,
             "results": results(("101", 0.5), ("102", 0.25))},
            {"case_id": "case_002", "best_rank": None, "results": results(("201", 0.125))},
        ]
        negatives = [{"case_id": "case_003", "results": results(("301", 0.0625))}]
        tracks[track] = {"field": field, "positives": positives, "negatives": negatives,
                         "metrics": run_eval.summarize([p["best_rank"] for p in positives])}
    header = {"cases_sha256": cases_sha256, "mode": mode,
              "base_url": "http://127.0.0.1:3000", "rows": {}}
    return run_eval.json_report(tracks, 2, header)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = self.dir / name
        if isinstance(data, (dict, list)):
            data = json.dumps(data, indent=2, allow_nan=False)
        path.write_text(data, encoding="utf-8")
        return path

    def run_main(self, old, new):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmp.main([str(self.write("old.json", old)),
                             str(self.write("new.json", new))])
        return code, out.getvalue().splitlines()

    def test_identical_reports(self):
        code, lines = self.run_main(report(), report())
        self.assertEqual((code, lines), (0, ["identical"]))

    def test_aggregate_equal_but_ids_swapped_is_a_difference(self):
        new = report()
        res = new["tracks"]["A"]["positives"][0]["results"]
        res[0]["sourceArticleId"], res[1]["sourceArticleId"] = "102", "101"
        code, lines = self.run_main(report(), new)
        self.assertEqual(code, 1)
        self.assertEqual(lines, [
            "case_001 track A: position 1 sourceArticleId '101' -> '102'",
            "case_001 track A: position 2 sourceArticleId '102' -> '101'",
            "2 differences",
        ])

    def test_score_compared_exactly(self):
        new = report()
        new["tracks"]["B"]["negatives"][0]["results"][0]["score"] = 0.0625000001
        code, lines = self.run_main(report(), new)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0],
                         "case_003 track B: position 1 score 0.0625 -> 0.0625000001")

    def test_int_and_float_score_differ_in_type_only_is_flagged(self):
        old, new = report(), report()
        old["tracks"]["A"]["positives"][1]["results"][0]["score"] = 1
        new["tracks"]["A"]["positives"][1]["results"][0]["score"] = True
        code, lines = self.run_main(old, new)
        self.assertEqual(code, 1)
        self.assertIn("case_002 track A: position 1 score 1 -> True", lines)

    def test_rank_difference(self):
        new = report()
        new["tracks"]["A"]["positives"][0]["results"][1]["rank"] = 3
        code, lines = self.run_main(report(), new)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "case_001 track A: position 2 rank 2 -> 3")

    def test_length_difference(self):
        new = report()
        new["tracks"]["B"]["positives"][0]["results"].pop()
        code, lines = self.run_main(report(), new)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "case_001 track B: length 2 -> 1")

    def test_header_differences(self):
        code, lines = self.run_main(report(), report(mode="semantic", cases_sha256="b" * 64))
        self.assertEqual(code, 1)
        self.assertEqual(lines[:2], ["header: mode lexical -> semantic",
                                     f"header: cases_sha256 {'a' * 64} -> {'b' * 64}"])

    def test_missing_extra_and_moved_cases(self):
        new = report()
        moved = new["tracks"]["A"]["positives"].pop(1)
        new["tracks"]["A"]["negatives"].append(moved)
        code, lines = self.run_main(report(), new)
        self.assertEqual(code, 1)
        self.assertIn("case_002 track A: missing from new positives", lines)
        self.assertIn("case_002 track A: extra in new negatives", lines)

    def test_case_order_difference(self):
        new = report()
        new["tracks"]["B"]["positives"].reverse()
        code, lines = self.run_main(report(), new)
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["track B positives: case order differs", "1 differences"])

    def test_missing_track(self):
        new = report()
        del new["tracks"]["B"]
        code, lines = self.run_main(report(), new)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "track B: missing in new")

    def test_output_never_includes_base_url_or_metrics(self):
        new = report()
        new["base_url"] = "http://example.invalid:9"
        new["tracks"]["A"]["metrics"]["top1"] = 99
        code, lines = self.run_main(report(), new)
        self.assertEqual((code, lines), (0, ["identical"]))

    def test_unreadable_reports_exit_2_without_content(self):
        bad = {
            "not json": "{not json SECRET_VALUE_9x",
            "nan score": '{"mode":"x","cases_sha256":"y","tracks":{"A":{"positives":'
                         '[{"case_id":"c","results":[{"rank":1,"sourceArticleId":"1",'
                         '"score":NaN}]}],"negatives":[]}}}',
            "top level list": [],
            "no tracks": {"mode": "x", "cases_sha256": "y"},
            "result missing score": self._without_score(),
        }
        for name, data in bad.items():
            with self.subTest(name):
                code, lines = self.run_main(data, report())
                self.assertEqual(code, 2)
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("error: old: "))
                self.assertNotIn("SECRET_VALUE", lines[0])

    def test_missing_file_exits_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmp.main([str(self.dir / "absent.json"), str(self.write("n.json", report()))])
        self.assertEqual(code, 2)
        self.assertTrue(out.getvalue().startswith("error: old: cannot read"))

    def test_inputs_are_not_modified(self):
        old = report()
        old_path, new_path = self.write("o.json", old), self.write("n.json", report())
        before = (old_path.read_bytes(), new_path.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            cmp.main([str(old_path), str(new_path)])
        self.assertEqual((old_path.read_bytes(), new_path.read_bytes()), before)

    @staticmethod
    def _without_score():
        r = copy.deepcopy(report())
        del r["tracks"]["A"]["positives"][0]["results"][0]["score"]
        return r


if __name__ == "__main__":
    unittest.main()
