"""Offline tests for scripts/run_eval.py. No real cases and no real server:
cases are built here from placeholder text, the client is either a fake or a
stub HTTP server on 127.0.0.1, and every query carries a sentinel that must
never show up in output, error messages or written files."""
import contextlib
import hashlib
import io
import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import run_eval  # noqa: E402

SENTINEL = "SENTINEL_QUERY_5v8t"
FIXTURE = ROOT / "tests" / "fixtures" / "fake_cases.json"


def query(cid, track):
    return f"{SENTINEL} track{track} {cid}"


def case(cid, targets=("101",), has_match=None, **over):
    c = {
        "id": cid,
        "customer_query": query(cid, "A"),
        "technician_query": query(cid, "B"),
        "target_article_ids": list(targets),
        "has_match": bool(targets) if has_match is None else has_match,
    }
    c.update(over)
    return c


def result(rank, sid):
    return {"rank": rank, "sourceArticleId": sid, "title": f"Title {sid}",
            "url": f"https://kb.example.test/{sid}", "score": round(1 / rank, 4)}


def response(sids, mode="lexical"):
    return {"mode": mode, "results": [result(i, s) for i, s in enumerate(sids, start=1)]}


def filler(n, start=1000):
    return [str(start + i) for i in range(n)]


class FakeClient:
    """responses: {(case_id, track): list of sourceArticleIds or a raw response}.
    Unlisted keys return no results. keys maps query text to (case_id, track) for
    queries not built by query()."""

    def __init__(self, responses=None, exc=None, keys=None):
        self.responses = responses or {}
        self.exc = exc
        self.keys = keys or {}
        self.calls = []

    def search(self, q, mode):
        if q in self.keys:
            key = self.keys[q]
        else:
            _, track, cid = q.split(" ")
            key = (cid, track[-1])
        self.calls.append((key, mode))
        if self.exc:
            raise self.exc
        r = self.responses.get(key, [])
        return response(r, mode) if isinstance(r, list) else r


# ------------------------------------------------------------------ stub server


class StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append({"path": self.path, "body": body,
                                     "content_type": self.headers.get("Content-Type")})
        status, headers, payload = self.server.reply(body)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class StubServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # e.g. the timeout test's client hangs up before the reply


def reply_ok(body):
    mode = json.loads(body)["mode"]
    return 200, {"Content-Type": "application/json"}, json.dumps(response(["101"], mode)).encode()


def reply_echo(status, content_type="application/json"):
    # Echoes the request body (which holds the query) back in the response.
    return lambda body: (status, {"Content-Type": content_type}, b'{"echo": ' + body + b"}")


def reply_non_json(body):
    return 200, {"Content-Type": "text/html"}, b"<html>" + body + b"</html>"


def reply_redirect(body):
    return 307, {"Location": "/elsewhere"}, b""


def reply_slow(body):
    time.sleep(1.0)
    return reply_ok(body)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_cases(self, data, name="cases.json"):
        p = self.dir / name
        p.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))
        return p

    def run_main(self, cases_path, args=(), client=None, mode="lexical"):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--mode", mode, "--cases", str(cases_path), *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = run_eval.main(argv, client=client)
            except SystemExit as e:  # argparse
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def run_cases(self, cases, args=(), client=None, mode="lexical"):
        return self.run_main(self.write_cases(cases), args, client, mode)

    def start_stub(self, reply):
        server = StubServer(("127.0.0.1", 0), StubHandler)
        server.reply = reply
        server.requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"http://127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------------ cases file


class ValidationTests(Base):
    def assert_invalid(self, cases, *expected):
        client = FakeClient()
        code, out, err = self.run_cases(cases, client=client)
        self.assertEqual(code, run_eval.EXIT_CASES, out)
        for e in expected:
            self.assertIn(e, out)
        self.assertIn("no requests sent", out)
        self.assertEqual(client.calls, [])
        self.assertNotIn(SENTINEL, out + err)
        return out

    def test_fixture_is_valid(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(run_eval.validate_cases(data), [])
        self.assertEqual([c["id"] for c in data], ["case_fake_001", "case_fake_002"])

    def test_has_match_true_with_empty_targets(self):
        self.assert_invalid([case("c1", targets=(), has_match=True)],
                            "case c1: has_match is true but target_article_ids is empty")

    def test_has_match_false_with_targets(self):
        self.assert_invalid([case("c1", targets=("101",), has_match=False)],
                            "case c1: has_match is false but target_article_ids is not empty")

    def test_numeric_target_is_invalid(self):
        out = self.assert_invalid([case("c1", target_article_ids=[28])],
                                  "case c1: target_article_ids must contain only strings")
        error_lines = [line for line in out.splitlines() if line.startswith("case ")]
        self.assertTrue(error_lines)
        self.assertNotIn("28", "\n".join(error_lines))

    def test_mixed_targets_are_not_coerced(self):
        self.assert_invalid([case("c1", target_article_ids=["101", 28])],
                            "case c1: target_article_ids must contain only strings")

    def test_targets_not_a_list(self):
        self.assert_invalid([case("c1", target_article_ids="101")],
                            "case c1: target_article_ids must be a list")

    def test_empty_string_target(self):
        self.assert_invalid([case("c1", target_article_ids=[""])],
                            "case c1: target_article_ids must not contain empty strings")

    def test_duplicate_targets(self):
        self.assert_invalid([case("c1", target_article_ids=["101", "101"])],
                            "case c1: target_article_ids has duplicates")

    def test_has_match_not_bool(self):
        self.assert_invalid([case("c1", has_match="true")],
                            "case c1: has_match must be true or false")

    def test_missing_field(self):
        c = case("c1")
        del c["technician_query"]
        self.assert_invalid([c], "case c1: missing field(s) technician_query")

    def test_extra_field_counted_not_named(self):
        c = case("c1")
        c[SENTINEL] = SENTINEL
        self.assert_invalid([c], "case c1: 1 unexpected field(s)")

    def test_duplicate_id(self):
        self.assert_invalid([case("c1"), case("c1")], "case c1: duplicate id")

    def test_missing_id_uses_index(self):
        c = case("c2")
        del c["id"]
        self.assert_invalid([case("c1"), c], "case at index 1: id must be")

    def test_unsafe_id_uses_index(self):
        self.assert_invalid([case("c1", id=f"{SENTINEL} with spaces")],
                            "case at index 0: id must be")

    def test_non_string_query(self):
        self.assert_invalid([case("c1", customer_query=5)],
                            "case c1: customer_query must be a string")

    def test_blank_query(self):
        self.assert_invalid([case("c1", technician_query="   ")],
                            "case c1: technician_query must not be blank")

    def test_query_too_long(self):
        self.assert_invalid([case("c1", customer_query=SENTINEL * 1000)],
                            "case c1: customer_query is longer than 5000 characters")

    def test_case_not_object(self):
        self.assert_invalid([case("c1"), [SENTINEL]], "case at index 1: must be an object")

    def test_top_level_not_list(self):
        self.assert_invalid({"cases": [case("c1")]}, "cases file must be a JSON array")

    def test_empty_list(self):
        self.assert_invalid([], "cases file has no cases")

    def test_all_errors_collected(self):
        out = self.assert_invalid(
            [case("c1", has_match="x"), case("c2", target_article_ids=[1])],
            "case c1: has_match must be true or false",
            "case c2: target_article_ids must contain only strings")
        self.assertIn("invalid cases file: 2 error(s)", out)

    def test_invalid_json(self):
        client = FakeClient()
        code, out, err = self.run_main(
            self.write_cases(b'[{"id": "c1", "customer_query": "' + SENTINEL.encode()),
            client=client)
        self.assertEqual(code, run_eval.EXIT_CASES)
        self.assertIn("cases file is not valid JSON (line 1 column", out)
        self.assertIn("cases sha256:", out)
        self.assertEqual(client.calls, [])
        self.assertNotIn(SENTINEL, out + err)

    def test_not_utf8(self):
        code, out, _ = self.run_main(self.write_cases(b"\xff\xfe[]"), client=FakeClient())
        self.assertEqual(code, run_eval.EXIT_CASES)
        self.assertIn("cases file is not UTF-8", out)

    def test_bom_accepted_and_hash_covers_raw_bytes(self):
        raw = b"\xef\xbb\xbf" + json.dumps([case("c1")]).encode("utf-8")
        code, out, _ = self.run_main(self.write_cases(raw), client=FakeClient())
        self.assertEqual(code, 0, out)
        self.assertIn(f"cases sha256: {hashlib.sha256(raw).hexdigest()}", out)


# ------------------------------------------------------------------ response checks


class ResponseCheckTests(unittest.TestCase):
    def assert_rejected(self, resp, message, mode="lexical"):
        with self.assertRaises(run_eval.ResponseCheckError) as cm:
            run_eval.check_response(resp, mode)
        self.assertIn(message, str(cm.exception))

    def test_valid_response_passes_unchanged(self):
        resp = response(["3", "1", "2"])
        self.assertIs(run_eval.check_response(resp, "lexical"), resp["results"])
        self.assertEqual([r["sourceArticleId"] for r in resp["results"]], ["3", "1", "2"])

    def test_empty_results_pass(self):
        self.assertEqual(run_eval.check_response(response([]), "lexical"), [])

    def test_fifty_results_pass(self):
        self.assertEqual(len(run_eval.check_response(response(filler(50)), "lexical")), 50)

    def test_not_an_object(self):
        self.assert_rejected([], "response is not a JSON object")

    def test_mode_mismatch(self):
        self.assert_rejected(response(["1"], mode="semantic"), "mode does not match")

    def test_mode_missing(self):
        self.assert_rejected({"results": []}, "mode does not match")

    def test_results_not_list(self):
        self.assert_rejected({"mode": "lexical", "results": {}}, "results is not a list")

    def test_more_than_fifty(self):
        self.assert_rejected(response(filler(51)), "51 results, more than 50")

    def test_result_not_object(self):
        self.assert_rejected({"mode": "lexical", "results": ["101"]}, "result 1 is not an object")

    def test_rank_gap(self):
        resp = response(["1", "2"])
        resp["results"][1]["rank"] = 3
        self.assert_rejected(resp, "result 2: rank 3 out of order (expected 2)")

    def test_shuffled_ranks_rejected_not_sorted(self):
        resp = response(["1", "2"])
        resp["results"].reverse()
        self.assert_rejected(resp, "result 1: rank 2 out of order (expected 1)")

    def test_rank_bool(self):
        resp = response(["1"])
        resp["results"][0]["rank"] = True
        self.assert_rejected(resp, "result 1: rank is not an integer")

    def test_rank_float(self):
        resp = response(["1"])
        resp["results"][0]["rank"] = 1.0
        self.assert_rejected(resp, "result 1: rank is not an integer")

    def test_numeric_source_article_id(self):
        resp = response(["28"])
        resp["results"][0]["sourceArticleId"] = 28
        self.assert_rejected(resp, "result 1: sourceArticleId is not a string")

    def test_duplicate_source_article_id(self):
        self.assert_rejected(response(["7", "7"]), "result 2: duplicate sourceArticleId")

    def test_missing_title(self):
        resp = response(["1"])
        del resp["results"][0]["title"]
        self.assert_rejected(resp, "result 1: title is not a string")

    def test_bad_scores(self):
        for bad in ("0.5", True, None, math.nan, math.inf):
            with self.subTest(score=bad):
                resp = response(["1"])
                resp["results"][0]["score"] = bad
                self.assert_rejected(resp, "result 1: score is not a finite number")


# ------------------------------------------------------------------ matching and metrics


class MatchingTests(unittest.TestCase):
    def results(self, sids):
        return response(sids)["results"]

    def test_rank_1(self):
        self.assertEqual(run_eval.best_rank(self.results(["101", "5"]), ["101"]), 1)

    def test_rank_50(self):
        self.assertEqual(run_eval.best_rank(self.results(filler(49) + ["101"]), ["101"]), 50)

    def test_miss(self):
        self.assertIsNone(run_eval.best_rank(self.results(filler(50)), ["101"]))

    def test_lowest_rank_of_multiple_targets(self):
        sids = filler(10)
        sids[6], sids[1] = "A", "B"  # A at rank 7, B at rank 2
        results = self.results(sids)
        self.assertEqual(run_eval.best_rank(results, ["A", "B"]), 2)
        self.assertEqual(run_eval.best_rank(results, ["B", "A"]), 2)
        m = run_eval.summarize([run_eval.best_rank(results, ["A", "B"])])
        self.assertEqual(m["mrr_at_50"], 0.5)

    def test_exact_string_match_only(self):
        self.assertIsNone(run_eval.best_rank(self.results(["028"]), ["28"]))
        self.assertIsNone(run_eval.best_rank(self.results(["28"]), ["028"]))
        self.assertIsNone(run_eval.best_rank(self.results(["28 "]), ["28"]))
        self.assertEqual(run_eval.best_rank(self.results(["028", "28"]), ["28"]), 2)

    def test_numeric_target_never_matches_string(self):
        # The cases file rejects numeric targets; this shows there is no coercion either.
        self.assertIsNone(run_eval.best_rank(self.results(["28"]), [28]))

    def test_summarize(self):
        m = run_eval.summarize([1, 3, 50, None])
        self.assertEqual((m["positives"], m["top1"], m["top3"], m["no_target_in_top_50"]),
                         (4, 1, 2, 1))
        self.assertAlmostEqual(m["mrr_at_50"], (1 + 1 / 3 + 1 / 50 + 0) / 4)

    def test_summarize_no_positives(self):
        m = run_eval.summarize([])
        self.assertIsNone(m["mrr_at_50"])
        self.assertEqual(run_eval.fmt_count(0, 0), "0/0 (n/a)")

    def test_fmt_count(self):
        self.assertEqual(run_eval.fmt_count(14, 21), "14/21 (67%)")
        self.assertEqual(run_eval.fmt_count(0, 1), "0/1 (0%)")


# ------------------------------------------------------------------ report


def section(out, track):
    a, b = out.split("== Track B")
    return a if track == "A" else b


class ReportTests(Base):
    def test_target_at_rank_1(self):
        code, out, _ = self.run_cases([case("c1")], client=FakeClient({("c1", "A"): ["101"]}))
        self.assertEqual(code, 0, out)
        a = section(out, "A")
        self.assertIn("Top-1: 1/1 (100%)   Top-3: 1/1 (100%)   MRR@50: 1.000 (N=1)", a)
        self.assertIn("No target in top 50: 0 / 1", a)
        self.assertIn("  c1  best target rank 1", a)

    def test_target_at_rank_50(self):
        client = FakeClient({("c1", "A"): filler(49) + ["101"]})
        code, out, _ = self.run_cases([case("c1")], client=client)
        self.assertEqual(code, 0, out)
        a = section(out, "A")
        self.assertIn("Top-1: 0/1 (0%)   Top-3: 0/1 (0%)   MRR@50: 0.020 (N=1)", a)
        self.assertIn("No target in top 50: 0 / 1", a)
        self.assertIn("  c1  best target rank 50", a)
        self.assertIn("observed depth: max 50 results per request (limit 50)", out)

    def test_miss(self):
        client = FakeClient({("c1", "A"): filler(50)})
        code, out, _ = self.run_cases([case("c1")], client=client)
        self.assertEqual(code, 0, out)
        a = section(out, "A")
        self.assertIn("MRR@50: 0.000 (N=1)", a)
        self.assertIn("No target in top 50: 1 / 1", a)
        self.assertIn("  c1  not in top 50", a)

    def test_multiple_targets_through_main(self):
        sids = filler(10)
        sids[6], sids[1] = "A", "B"
        for targets in (("A", "B"), ("B", "A")):
            with self.subTest(targets=targets):
                client = FakeClient({("c1", "A"): sids})
                code, out, _ = self.run_cases([case("c1", targets=targets)], client=client)
                self.assertEqual(code, 0, out)
                self.assertIn("MRR@50: 0.500 (N=1)", section(out, "A"))
                self.assertIn("  c1  best target rank 2", section(out, "A"))

    def test_tracks_reported_separately(self):
        client = FakeClient({("c1", "A"): ["101"], ("c1", "B"): filler(5),
                             ("c2", "A"): filler(5), ("c2", "B"): filler(2) + ["202"]})
        cases = [case("c1"), case("c2", targets=("202",))]
        code, out, _ = self.run_cases(cases, client=client)
        self.assertEqual(code, 0, out)
        a, b = section(out, "A"), section(out, "B")
        self.assertIn("== Track A (customer_query) ==", a)
        self.assertIn("Top-1: 1/2 (50%)   Top-3: 1/2 (50%)   MRR@50: 0.500 (N=2)", a)
        self.assertIn("  c1  best target rank 1", a)
        self.assertIn("  c2  not in top 50", a)
        self.assertIn("(technician_query) ==", b)
        self.assertIn("Top-1: 0/2 (0%)   Top-3: 1/2 (50%)   MRR@50: 0.167 (N=2)", b)
        self.assertIn("  c1  not in top 50", b)
        self.assertIn("  c2  best target rank 3", b)

    def test_negatives_are_report_only(self):
        client = FakeClient({("n1", "A"): ["555", "1", "2", "3", "4", "5", "6"],
                             ("c1", "A"): ["101"]})
        cases = [case("c1"), case("n1", targets=())]
        code, out, _ = self.run_cases(cases, client=client)
        self.assertEqual(code, 0, out)
        a, b = section(out, "A"), section(out, "B")
        self.assertIn("Negatives (qualitative review only):", a)
        self.assertIn('  n1  top: 555 "Title 555" score 1.0000', a)
        self.assertIn("    top 5: Title 555 | Title 1 | Title 2 | Title 3 | Title 4", a)
        self.assertNotIn("Title 5\n", a)
        self.assertIn("  n1  no results", b)
        # Negatives never count toward the positive metrics.
        self.assertIn("MRR@50: 1.000 (N=1)", a)
        for word in ("pass", "fail", "precision", "recall"):
            self.assertNotIn(word, out.lower())

    def test_no_negatives(self):
        code, out, _ = self.run_cases([case("c1")], client=FakeClient())
        self.assertEqual(code, 0, out)
        self.assertIn("Negatives (qualitative review only):\n  (none)", out)

    def test_mrr_always_labeled_at_50(self):
        code, out, _ = self.run_cases([case("c1"), case("n1", targets=())],
                                      client=FakeClient())
        self.assertEqual(code, 0, out)
        self.assertIn("MRR@50", out)
        self.assertEqual(out.count("MRR"), out.count("MRR@50"))

    def test_header(self):
        path = self.write_cases([case("c1"), case("n1", targets=())])
        code, out, _ = self.run_main(path, client=FakeClient())
        self.assertEqual(code, 0, out)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIn(f"cases sha256: {digest}   file: {path}", out)
        self.assertIn("mode: lexical   base url: http://127.0.0.1:3000", out)
        self.assertIn("rows: 2 cases (1 positive, 1 negative), 4 requests", out)
        self.assertIn("observed depth: max 0 results per request (limit 50)", out)

    def test_request_order_and_mode(self):
        client = FakeClient()
        code, _, _ = self.run_cases([case("c1"), case("c2")], client=client, mode="semantic")
        self.assertEqual(code, 0)
        self.assertEqual(client.calls, [(("c1", "A"), "semantic"), (("c1", "B"), "semantic"),
                                        (("c2", "A"), "semantic"), (("c2", "B"), "semantic")])

    def test_fixture_scores_zero(self):
        keys = {}
        for c in json.loads(FIXTURE.read_text(encoding="utf-8")):
            keys[c["customer_query"]] = (c["id"], "A")
            keys[c["technician_query"]] = (c["id"], "B")
        client = FakeClient({("case_fake_001", "A"): filler(50), ("case_fake_001", "B"): filler(3),
                             ("case_fake_002", "A"): filler(5)}, keys=keys)
        code, out, _ = self.run_main(FIXTURE, client=client)
        self.assertEqual(code, 0, out)
        self.assertIn("rows: 2 cases (1 positive, 1 negative), 4 requests", out)
        for track in ("A", "B"):
            s = section(out, track)
            self.assertIn("Top-1: 0/1 (0%)   Top-3: 0/1 (0%)   MRR@50: 0.000 (N=1)", s)
            self.assertIn("No target in top 50: 1 / 1", s)
            self.assertIn("  case_fake_001  not in top 50", s)
            self.assertIn("  case_fake_002  ", s)

    def test_shape_failure_stops_run(self):
        bad = response(["1"], mode="semantic")
        client = FakeClient({("c1", "B"): bad})
        code, out, _ = self.run_cases([case("c1"), case("c2")], client=client)
        self.assertEqual(code, run_eval.EXIT_RESPONSE)
        self.assertIn("error: response check failed: case c1 track B: mode does not match", out)
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("== Track A", out)

    def test_unexpected_error(self):
        code, out, err = self.run_cases([case("c1")], client=FakeClient(exc=RuntimeError(SENTINEL)))
        self.assertEqual(code, run_eval.EXIT_UNEXPECTED)
        self.assertIn("unexpected error: RuntimeError", out)
        self.assertNotIn(SENTINEL, out + err)


# ------------------------------------------------------------------ arguments


class ArgumentTests(Base):
    def test_cases_is_required(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                run_eval.main(["--mode", "lexical"], client=FakeClient())
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--cases", err.getvalue())

    def test_mode_is_required(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                run_eval.main(["--cases", str(FIXTURE)], client=FakeClient())
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--mode", err.getvalue())

    def test_cases_not_found(self):
        code, out, _ = self.run_main(self.dir / "missing.json", client=FakeClient())
        self.assertEqual(code, run_eval.EXIT_USAGE)
        self.assertIn("cases file not found", out)

    def test_non_local_base_url_refused(self):
        for url in ("http://example.com:3000", "https://kb.example.org",
                    "http://10.0.0.5:3000", "http://127.0.0.1.example.com"):
            with self.subTest(url=url):
                client = FakeClient()
                code, out, err = self.run_cases([case("c1")], ["--base-url", url], client)
                self.assertEqual(code, run_eval.EXIT_USAGE)
                self.assertIn("host is not local", out)
                self.assertIn("--allow-remote", out)
                self.assertEqual(client.calls, [])
                self.assertNotIn(SENTINEL, out + err)

    def test_non_local_base_url_allowed_with_flag(self):
        client = FakeClient()
        code, out, _ = self.run_cases(
            [case("c1")], ["--base-url", "https://kb.example.org", "--allow-remote"], client)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(client.calls), 2)

    def test_local_base_urls_allowed(self):
        for url in ("http://localhost:3000", "http://127.0.0.1:3000/", "http://[::1]:3000"):
            with self.subTest(url=url):
                code, out, _ = self.run_cases([case("c1")], ["--base-url", url], FakeClient())
                self.assertEqual(code, 0, out)

    def test_malformed_base_urls_refused(self):
        for url in ("ftp://127.0.0.1", "127.0.0.1:3000", "http://user:pw@127.0.0.1:3000",
                    "http://127.0.0.1:3000/?x=1", "http://127.0.0.1:3000/#x"):
            with self.subTest(url=url):
                client = FakeClient()
                code, out, _ = self.run_cases([case("c1")], ["--base-url", url], client)
                self.assertEqual(code, run_eval.EXIT_USAGE, out)
                self.assertEqual(client.calls, [])


# ------------------------------------------------------------------ JSON output


def all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_keys(v)


class JsonOutTests(Base):
    def test_writes_report(self):
        sids = filler(10)
        sids[6], sids[1] = "A", "B"
        client = FakeClient({("c1", "A"): sids, ("n1", "A"): ["9"]})
        path = self.write_cases([case("c1", targets=("A", "B")), case("n1", targets=())])
        out_path = self.dir / "results.json"
        code, out, _ = self.run_main(path, ["--json-out", str(out_path)], client)
        self.assertEqual(code, 0, out)
        self.assertIn(f"wrote {out_path}", out)
        report = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["cases_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(report["mode"], "lexical")
        self.assertEqual(report["base_url"], "http://127.0.0.1:3000")
        self.assertEqual(report["observed_max_depth"], 10)
        self.assertEqual(report["rows"], {"cases": 2, "positives": 1, "negatives": 1,
                                          "requests": 4})
        a = report["tracks"]["A"]
        self.assertEqual(a["field"], "customer_query")
        self.assertEqual(a["metrics"]["mrr_at_50"], 0.5)
        pos = a["positives"][0]
        self.assertEqual((pos["case_id"], pos["best_rank"], pos["reciprocal_rank"]),
                         ("c1", 2, 0.5))
        self.assertEqual(pos["results"][1], {"rank": 2, "sourceArticleId": "B", "score": 0.5})
        self.assertEqual(a["negatives"][0]["case_id"], "n1")
        b = report["tracks"]["B"]
        self.assertEqual((b["positives"][0]["best_rank"], b["positives"][0]["reciprocal_rank"]),
                         (None, 0.0))
        keys = set(all_keys(report))
        for forbidden in ("title", "url", "target_article_ids", "customer_query",
                          "technician_query", "query"):
            self.assertNotIn(forbidden, keys)

    def test_refuses_existing_file(self):
        out_path = self.dir / "results.json"
        out_path.write_text("keep", encoding="utf-8")
        client = FakeClient()
        code, out, _ = self.run_cases([case("c1")], ["--json-out", str(out_path)], client)
        self.assertEqual(code, run_eval.EXIT_USAGE)
        self.assertIn("refusing to overwrite", out)
        self.assertEqual(out_path.read_text(encoding="utf-8"), "keep")
        self.assertEqual(client.calls, [])

    def test_refuses_missing_directory(self):
        code, out, _ = self.run_cases(
            [case("c1")], ["--json-out", str(self.dir / "nope" / "r.json")], FakeClient())
        self.assertEqual(code, run_eval.EXIT_USAGE)
        self.assertIn("directory does not exist", out)

    def test_no_file_on_failure(self):
        out_path = self.dir / "results.json"
        client = FakeClient({("c2", "A"): {"mode": "lexical"}})
        code, _, _ = self.run_cases([case("c1"), case("c2")],
                                    ["--json-out", str(out_path)], client)
        self.assertEqual(code, run_eval.EXIT_RESPONSE)
        self.assertFalse(out_path.exists())


# ------------------------------------------------------------------ HTTP client


class HttpClientTests(Base):
    def test_request_shape(self):
        server, url = self.start_stub(reply_ok)
        code, out, _ = self.run_cases([case("c1")], ["--base-url", url])
        self.assertEqual(code, 0, out)
        self.assertIn("  c1  best target rank 1", section(out, "A"))
        self.assertEqual(len(server.requests), 2)
        for req, track in zip(server.requests, ("A", "B")):
            self.assertEqual(req["path"], "/api/search")
            self.assertEqual(req["content_type"], "application/json")
            self.assertEqual(json.loads(req["body"]), {"query": query("c1", track),
                                                       "mode": "lexical"})

    def test_http_400(self):
        server, url = self.start_stub(reply_echo(400))
        code, out, _ = self.run_cases([case("c1"), case("c2")], ["--base-url", url])
        self.assertEqual(code, run_eval.EXIT_SERVER)
        self.assertIn("error: case c1 track A: HTTP 400 (request rejected", out)
        self.assertEqual(len(server.requests), 1)

    def test_http_500(self):
        _, url = self.start_stub(reply_echo(500))
        code, out, _ = self.run_cases([case("c1")], ["--base-url", url])
        self.assertEqual(code, run_eval.EXIT_SERVER)
        self.assertIn("error: case c1 track A: HTTP 500 (server error", out)

    def test_non_json(self):
        _, url = self.start_stub(reply_non_json)
        code, out, _ = self.run_cases([case("c1")], ["--base-url", url])
        self.assertEqual(code, run_eval.EXIT_SERVER)
        self.assertIn("error: case c1 track A: response is not JSON", out)

    def test_redirect_not_followed(self):
        server, url = self.start_stub(reply_redirect)
        code, out, _ = self.run_cases([case("c1")], ["--base-url", url])
        self.assertEqual(code, run_eval.EXIT_SERVER)
        self.assertIn("HTTP 307", out)
        self.assertEqual([r["path"] for r in server.requests], ["/api/search"])

    def test_connection_refused(self):
        url = f"http://127.0.0.1:{free_port()}"
        code, out, _ = self.run_cases([case("c1")], ["--base-url", url])
        self.assertEqual(code, run_eval.EXIT_SERVER)
        self.assertIn(f"error: case c1 track A: cannot reach {url}/api/search", out)
        self.assertIn("is the app running (npm run dev)?", out)

    def test_timeout(self):
        _, url = self.start_stub(reply_slow)
        client = run_eval.SearchClient(url, timeout=0.2)
        with self.assertRaises(run_eval.ServerError) as cm:
            client.search(query("c1", "A"), "lexical")
        self.assertIn("cannot reach", str(cm.exception))
        self.assertNotIn(SENTINEL, str(cm.exception))

    def test_proxy_settings_ignored(self):
        server, url = self.start_stub(reply_ok)
        dead = f"http://127.0.0.1:{free_port()}"
        env = {"HTTP_PROXY": dead, "http_proxy": dead, "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict(os.environ, env):
            code, out, _ = self.run_cases([case("c1")], ["--base-url", url])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(server.requests), 2)


# ------------------------------------------------------------------ privacy


class SentinelTests(Base):
    """The query text never appears in stdout, stderr or a written file."""

    def check(self, name, code, out, err, expected_code, written=None):
        with self.subTest(scenario=name):
            self.assertEqual(code, expected_code, out)
            self.assertNotIn(SENTINEL, out)
            self.assertNotIn(SENTINEL, err)
            if written is not None:
                self.assertTrue(written.exists())
                self.assertNotIn(SENTINEL, written.read_text(encoding="utf-8"))

    def test_sentinel_never_leaks(self):
        cases = [case("c1"), case("n1", targets=())]

        out_path = self.dir / "results.json"
        client = FakeClient({("c1", "A"): ["101"], ("n1", "A"): ["7", "8"]})
        self.check("success with --json-out",
                   *self.run_cases(cases, ["--json-out", str(out_path)], client),
                   0, written=out_path)

        bad = case("c1", target_article_ids=[28], has_match=SENTINEL)
        bad[SENTINEL] = SENTINEL
        self.check("invalid cases",
                   *self.run_cases([bad, case(f"{SENTINEL} id"), case("c2", customer_query="")],
                                   client=FakeClient()),
                   run_eval.EXIT_CASES)

        self.check("invalid JSON",
                   *self.run_main(self.write_cases(b'[{"q": "' + SENTINEL.encode()),
                                  client=FakeClient()),
                   run_eval.EXIT_CASES)

        self.check("shape failure",
                   *self.run_cases(cases, client=FakeClient({("c1", "A"): {"mode": SENTINEL}})),
                   run_eval.EXIT_RESPONSE)

        for status in (400, 500):
            _, url = self.start_stub(reply_echo(status))
            self.check(f"HTTP {status}", *self.run_cases(cases, ["--base-url", url]),
                       run_eval.EXIT_SERVER)

        _, url = self.start_stub(reply_non_json)
        self.check("non-JSON", *self.run_cases(cases, ["--base-url", url]),
                   run_eval.EXIT_SERVER)

        self.check("connection refused",
                   *self.run_cases(cases, ["--base-url", f"http://127.0.0.1:{free_port()}"]),
                   run_eval.EXIT_SERVER)

        self.check("non-local base url",
                   *self.run_cases(cases, ["--base-url", "http://example.com"], FakeClient()),
                   run_eval.EXIT_USAGE)

        self.check("unexpected error",
                   *self.run_cases(cases, client=FakeClient(exc=ValueError(SENTINEL))),
                   run_eval.EXIT_UNEXPECTED)


# ------------------------------------------------------------------ source guard


class SourceGuardTests(unittest.TestCase):
    def test_no_path_into_the_real_cases_directory(self):
        # Built by concatenation so this file does not contain the literal.
        needles = ("eval" + "/", "eval" + "\\")
        for path in (ROOT / "scripts" / "run_eval.py", Path(__file__)):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                self.assertNotIn(needle, text, path.name)


if __name__ == "__main__":
    unittest.main()
