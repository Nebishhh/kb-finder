"""Offline tests for scripts/paired_eval.py. Reports are synthetic and built
with run_eval.json_report, so the tests follow the real --json-out format. No
real eval results are read."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import paired_eval as pe  # noqa: E402
import run_eval  # noqa: E402


def report(ranks_a, ranks_b=None, mode="semantic", cases_sha256="a" * 64):
    """One positive per rank, named case_001.. in order, plus one negative."""
    tracks = {}
    for (track, field), ranks in zip(run_eval.TRACKS, (ranks_a, ranks_b or ranks_a)):
        positives = [{"case_id": f"case_{i:03d}", "best_rank": rank,
                      "results": [{"rank": 1, "sourceArticleId": "101", "title": "T",
                                   "url": "u", "score": 0.5}]}
                     for i, rank in enumerate(ranks, start=1)]
        negatives = [{"case_id": "case_900", "results": []}]
        tracks[track] = {"field": field, "positives": positives, "negatives": negatives,
                         "metrics": run_eval.summarize(list(ranks))}
    header = {"cases_sha256": cases_sha256, "mode": mode,
              "base_url": "http://127.0.0.1:3000", "rows": {}}
    return run_eval.json_report(tracks, 1, header)


def v2_report(ranks_a, mode="semantic"):
    """Track A as report(); Track B empty, as run_eval.py writes it when every
    technician_query is null."""
    r = report(ranks_a, mode=mode)
    r["tracks"]["B"].update(positives=[], negatives=[],
                            metrics=run_eval.summarize([]),
                            skipped_null_query=len(ranks_a) + 1)
    return r


class SignTestTests(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(pe.sign_test_p(0, 0), 1.0)
        self.assertEqual(pe.sign_test_p(6, 0), 2 / 64)
        self.assertEqual(pe.sign_test_p(0, 6), 2 / 64)
        self.assertEqual(pe.sign_test_p(8, 1), 2 * 10 / 512)
        self.assertEqual(pe.sign_test_p(7, 1), 2 * 9 / 256)
        self.assertEqual(pe.sign_test_p(5, 0), 2 / 32)
        self.assertEqual(pe.sign_test_p(3, 3), 1.0)

    def test_the_bar_is_the_frozen_rule_and_agrees_with_the_test_at_its_edges(self):
        for wins, losses, met in [(6, 0, True), (5, 0, False), (8, 1, True),
                                  (7, 1, False), (9, 2, False), (21, 0, True),
                                  (8, 0, True), (20, 1, True), (6, 1, False)]:
            with self.subTest(wins=wins, losses=losses):
                self.assertEqual(pe.meets_bar(wins, losses), met)
        self.assertLess(pe.sign_test_p(6, 0), 0.05)
        self.assertLess(pe.sign_test_p(8, 1), 0.05)
        self.assertGreater(pe.sign_test_p(5, 0), 0.05)
        self.assertGreater(pe.sign_test_p(7, 1), 0.05)

    def test_eval_v2_bar_is_the_sign_test_at_5_percent(self):
        fewest_wins = []
        for losses in range(6):
            wins = losses + 1
            while pe.bar_eval_v2(wins, losses) != "met":
                wins += 1
            fewest_wins.append(wins)
        self.assertEqual(fewest_wins, [6, 8, 10, 12, 13, 15])
        self.assertEqual(pe.bar_eval_v2(5, 0), "not met")
        self.assertEqual(pe.bar_eval_v2(0, 6), "met in reverse (real loss)")
        self.assertEqual(pe.bar_eval_v2(0, 0), "not met")


class PairedTests(unittest.TestCase):
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

    def run_main(self, base, new, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pe.main([str(self.write("base.json", base)),
                            str(self.write("new.json", new)), *args])
        return code, out.getvalue().splitlines()

    def test_explicit_default_rule_matches_no_flag(self):
        base, new = report([1, 2, 4, None]), report([2, 1, 3, 40], mode="hybrid")
        self.assertEqual(self.run_main(base, new, "--rule", "hybrid-rrf-v1"),
                         self.run_main(base, new))

    def test_eval_v2_rule_on_a_track_a_only_report(self):
        code, lines = self.run_main(v2_report([4, 1, 2]),
                                    v2_report([3, 1, 1], mode="hybrid"),
                                    "--rule", "eval-v2")
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            "base: mode semantic   new: mode hybrid",
            f"cases sha256: {'a' * 64}",
            "bar (eval-v2): more wins than losses and exact two-sided sign test "
            "p < 0.05 (6-0, 8-1, 10-2, 12-3, 13-4, 15-5); a net change of 1 to 3 "
            "cases is noise",
            "",
            "== Track A (customer_query), 3 positives ==",
            "Top-1 (secondary): wins 1, losses 0, ties 2, net +1   "
            "sign test p = 1.000   bar: not met",
            "  win   case_003  rank 2 -> 1",
            "Top-3 (primary): wins 1, losses 0, ties 2, net +1   "
            "sign test p = 1.000   bar: not met",
            "  win   case_001  rank 4 -> 3",
            "Rank direction (descriptive only): better 2, worse 0, same 1",
            "Regression check (net loss of 4 or more, or a target leaving the "
            "top 50): none",
            "",
            "== Track B (technician_query), 0 positives ==",
            "No positives on this track: nothing to compare.",
        ])

    def test_eval_v2_bar_on_the_primary(self):
        # A Top-3 win is rank 4 -> 3, a loss is 3 -> 4, a tie is 1 -> 1.
        for wins, losses, expected in [
            (6, 0, "sign test p = 0.031   bar: met"),
            (5, 0, "sign test p = 0.062   bar: not met"),
            (10, 2, "sign test p = 0.039   bar: met"),
            (9, 2, "sign test p = 0.065   bar: not met"),
            (13, 4, "sign test p = 0.049   bar: met"),
            (0, 6, "sign test p = 0.031   bar: met in reverse (real loss)"),
        ]:
            with self.subTest(wins=wins, losses=losses):
                base = [4] * wins + [3] * losses + [1]
                new = [3] * wins + [4] * losses + [1]
                code, lines = self.run_main(v2_report(base),
                                            v2_report(new, mode="hybrid"),
                                            "--rule", "eval-v2")
                self.assertEqual(code, 0)
                self.assertIn(f"Top-3 (primary): wins {wins}, losses {losses}, ties 1, "
                              f"net {wins - losses:+d}   {expected}", lines)

    def test_default_rule_refuses_a_report_with_an_empty_track_b(self):
        code, lines = self.run_main(v2_report([1, 2]), v2_report([2, 1], mode="hybrid"))
        self.assertEqual((code, lines), (2, [
            "error: rule hybrid-rrf-v1: its primary track B has no positives; "
            "pass the rule these reports were made for with --rule"]))

    def test_identical_ranks_are_all_ties(self):
        ranks = [1, 2, 4, None]
        code, lines = self.run_main(report(ranks), report(ranks, mode="hybrid"))
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "base: mode semantic   new: mode hybrid")
        self.assertEqual(lines[1], f"cases sha256: {'a' * 64}")
        self.assertIn("== Track A (customer_query), 4 positives ==", lines)
        self.assertIn("== Track B (technician_query), 4 positives ==", lines)
        self.assertEqual(
            [l for l in lines if l.startswith("Top-1")],
            ["Top-1 (secondary): wins 0, losses 0, ties 4, net +0   "
             "sign test p = 1.000   bar: not met",
             "Top-1 (primary): wins 0, losses 0, ties 4, net +0   "
             "sign test p = 1.000   bar: not met"])
        self.assertEqual(len([l for l in lines if l.startswith("Top-3 (secondary)")]), 2)
        self.assertIn("Rank direction (descriptive only): better 0, worse 0, same 4", lines)
        self.assertFalse([l for l in lines if l.startswith("  win") or l.startswith("  loss")])

    def test_wins_and_losses_are_counted_per_cutoff_and_listed_by_case(self):
        #            001 002 003 004   005
        base_b = [2, 1, 4, 3, None]
        new_b = [1, 2, 3, 5, 40]
        code, lines = self.run_main(report([1] * 5, base_b),
                                    report([1] * 5, new_b, mode="hybrid"))
        self.assertEqual(code, 0)
        start = lines.index("== Track B (technician_query), 5 positives ==")
        self.assertEqual(lines[start + 1:], [
            "Top-1 (primary): wins 1, losses 1, ties 3, net +0   "
            "sign test p = 1.000   bar: not met",
            "  win   case_001  rank 2 -> 1",
            "  loss  case_002  rank 1 -> 2",
            "Top-3 (secondary): wins 1, losses 1, ties 3, net +0   "
            "sign test p = 1.000   bar: not met",
            "  win   case_003  rank 4 -> 3",
            "  loss  case_004  rank 3 -> 5",
            "Rank direction (descriptive only): better 3, worse 2, same 0",
        ])

    def test_six_wins_and_no_losses_meets_the_bar_on_the_primary(self):
        base_b = [2] * 6 + [1, 5]
        new_b = [1] * 6 + [1, 5]
        code, lines = self.run_main(report([1] * 8, base_b),
                                    report([1] * 8, new_b, mode="hybrid"))
        self.assertEqual(code, 0)
        self.assertIn("Top-1 (primary): wins 6, losses 0, ties 2, net +6   "
                      "sign test p = 0.031   bar: met", lines)

    def test_five_wins_or_seven_wins_one_loss_does_not_meet_the_bar(self):
        for base_b, new_b, expected in [
            ([2] * 5 + [1], [1] * 5 + [1],
             "Top-1 (primary): wins 5, losses 0, ties 1, net +5   "
             "sign test p = 0.062   bar: not met"),
            ([2] * 7 + [1], [1] * 7 + [2],
             "Top-1 (primary): wins 7, losses 1, ties 0, net +6   "
             "sign test p = 0.070   bar: not met"),
            ([2] * 8 + [1], [1] * 8 + [2],
             "Top-1 (primary): wins 8, losses 1, ties 0, net +7   "
             "sign test p = 0.039   bar: met"),
        ]:
            with self.subTest(expected=expected):
                n = len(base_b)
                code, lines = self.run_main(report([1] * n, base_b),
                                            report([1] * n, new_b, mode="hybrid"))
                self.assertEqual(code, 0)
                self.assertIn(expected, lines)

    def regression_line(self, base_a, new_a):
        n = len(base_a)
        code, lines = self.run_main(report(base_a, [1] * n),
                                    report(new_a, [1] * n, mode="hybrid"))
        self.assertEqual(code, 0)
        found = [l for l in lines if l.startswith("Regression check")]
        self.assertEqual(len(found), 1)  # Track A only
        return found[0].split("): ", 1)[1]

    def test_track_a_net_loss_of_three_is_not_a_regression(self):
        self.assertEqual(self.regression_line([1, 1, 1, 9], [2, 2, 2, 9]), "none")

    def test_track_a_net_loss_of_four_on_top1_is_a_regression(self):
        self.assertEqual(self.regression_line([1, 1, 1, 1, 9], [2, 2, 2, 2, 9]),
                         "REGRESSION: Top-1 net loss 4")

    def test_track_a_net_loss_is_net_of_wins(self):
        # 5 losses and 2 wins on Top-3: net 3, inside the noise band.
        self.assertEqual(
            self.regression_line([3, 3, 3, 3, 3, 9, 9], [4, 4, 4, 4, 4, 2, 2]), "none")

    def test_track_a_target_leaving_the_top_50_is_a_regression(self):
        self.assertEqual(self.regression_line([1, 30, None], [1, None, None]),
                         "REGRESSION: left the top 50: case_002")

    def test_track_b_losses_never_print_a_regression_line(self):
        code, lines = self.run_main(report([1] * 5, [1] * 5),
                                    report([1] * 5, [9] * 5, mode="hybrid"))
        self.assertEqual(code, 0)
        self.assertEqual([l for l in lines if l.startswith("Regression check")],
                         ["Regression check (net loss of 4 or more, or a target leaving "
                          "the top 50): none"])

    def test_same_mode_is_allowed(self):
        code, _ = self.run_main(report([1, 2]), report([2, 1]))
        self.assertEqual(code, 0)

    def test_reports_that_cannot_be_paired_exit_2(self):
        reordered = report([1, 2], mode="hybrid")
        reordered["tracks"]["A"]["positives"].reverse()
        one_track = report([1, 2], mode="hybrid")
        del one_track["tracks"]["B"]
        bad_rank = report([1, 2], mode="hybrid")
        bad_rank["tracks"]["B"]["positives"][0]["best_rank"] = 0
        bool_rank = report([1, 2], mode="hybrid")
        bool_rank["tracks"]["B"]["positives"][0]["best_rank"] = True
        for name, new, message in [
            ("other cases file", report([1, 2], mode="hybrid", cases_sha256="b" * 64),
             "error: reports were made from different cases files (cases_sha256 differs)"),
            ("fewer cases", report([1], mode="hybrid"),
             "error: track A: positives are not the same cases in the same order"),
            ("case order", reordered,
             "error: track A: positives are not the same cases in the same order"),
            ("missing track", one_track, "error: reports do not have the same tracks"),
            ("rank zero", bad_rank,
             "error: new: case_001 track B: best_rank is not null or a positive integer"),
            ("boolean rank", bool_rank,
             "error: new: case_001 track B: best_rank is not null or a positive integer"),
        ]:
            with self.subTest(name):
                code, lines = self.run_main(report([1, 2]), new)
                self.assertEqual((code, lines), (2, [message]))

    def test_unreadable_report_exits_2_without_content(self):
        code, lines = self.run_main("{not json SECRET_VALUE_9x", report([1]))
        self.assertEqual(code, 2)
        self.assertEqual(lines, ["error: base: not valid JSON"])

    def test_missing_file_exits_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pe.main([str(self.dir / "absent.json"),
                            str(self.write("n.json", report([1])))])
        self.assertEqual(code, 2)
        self.assertTrue(out.getvalue().startswith("error: base: cannot read"))

    def test_output_holds_no_base_url_article_ids_or_scores(self):
        new = report([2, 1], mode="hybrid")
        new["base_url"] = "http://example.invalid:9"
        code, lines = self.run_main(report([1, 2]), new)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        for absent in ("example.invalid", "127.0.0.1", "101", "0.5", "score"):
            self.assertNotIn(absent, text)

    def test_inputs_are_not_modified(self):
        base_path = self.write("b.json", report([1, 2]))
        new_path = self.write("n.json", report([2, 1], mode="hybrid"))
        before = (base_path.read_bytes(), new_path.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            pe.main([str(base_path), str(new_path)])
        self.assertEqual((base_path.read_bytes(), new_path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
