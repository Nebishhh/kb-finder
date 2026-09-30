#!/usr/bin/env python
"""Phase 4: evaluate a search mode through POST /api/search.

Input : --cases PATH, a JSON array of cases (schema in SPEC.md, Evaluation).
        There is no default path.
Server: --base-url (default http://127.0.0.1:3000). Every case's query text is
        sent to that server, so hosts other than localhost / 127.0.0.1 / ::1
        are refused unless --allow-remote is given.

Run:  .venv/Scripts/python scripts/run_eval.py --mode MODE --cases PATH
          [--base-url URL] [--allow-remote] [--json-out PATH]

Rules
-----
Retrieval    Only POST /api/search. This script does no ranking, sorting,
             scoring or SQL. It reads the ranks the API returns, in the order
             returned, and never reorders them.
Validation   The whole cases file is checked before any request. Errors name
             the case by ID (or by index when the ID is unusable) plus the
             field and rule, never the value.
Response     Each response is checked: mode matches, at most 50 results, ranks
             1..n in order, sourceArticleId and title are strings, score is a
             finite number, no duplicate sourceArticleId.
Matching     A hit is exact string equality between a returned sourceArticleId
             and a target ID. With several targets, the case scores the lowest
             returned rank of any of them.
Metrics      Per track, positives only: Top-1, Top-3, MRR@50 (a target outside
             the returned results scores 0) and the count of positives with no
             target in the top 50. Negatives are printed for qualitative review
             only.
Output       Case IDs, ranks, scores, article IDs and titles. Query text is never
             printed, written or put in an error message. --json-out writes
             case IDs, ranks, scores and article IDs only (no titles, no target
             IDs) and never overwrites an existing file.
Errors       The first failure stops the run: no partial metrics, no JSON file.
             No retries.
Exit codes   0 ok, 1 invalid cases file, 2 usage/config, 3 server unreachable or
             HTTP error, 4 response check failed, 5 unexpected error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_RESULTS = 50  # frozen server-side depth (DECISIONS.md, 2026-09-30)
MAX_QUERY_CHARS = 5000  # SearchRequestSchema limit
REQUEST_TIMEOUT_S = 30.0
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
CASE_KEYS = ("id", "customer_query", "technician_query", "target_article_ids", "has_match")
# Case IDs are printed, so only plain identifiers are accepted.
CASE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TRACKS = (("A", "customer_query"), ("B", "technician_query"))

EXIT_OK = 0
EXIT_CASES = 1
EXIT_USAGE = 2
EXIT_SERVER = 3
EXIT_RESPONSE = 4
EXIT_UNEXPECTED = 5


class EvalError(Exception):
    """A failure with a message that is safe to print (no query text)."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class ServerError(Exception):
    """Raised by the client. The message never contains request or response data."""


class ResponseCheckError(Exception):
    """Raised by check_response. The message names only the rule broken."""


# ------------------------------------------------------------------ cases


def parse_cases(raw: bytes) -> object:
    """Parse the same bytes that were hashed."""
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise EvalError("cases file is not UTF-8", EXIT_CASES) from None
    except json.JSONDecodeError as exc:
        raise EvalError(f"cases file is not valid JSON (line {exc.lineno} "
                        f"column {exc.colno})", EXIT_CASES) from None


def validate_cases(data: object) -> list[str]:
    if not isinstance(data, list):
        return ["cases file must be a JSON array of cases"]
    if not data:
        return ["cases file has no cases"]
    errors: list[str] = []
    seen: set[str] = set()
    for i, case in enumerate(data):
        if not isinstance(case, dict):
            errors.append(f"case at index {i}: must be an object")
            continue
        cid = case.get("id")
        if isinstance(cid, str) and CASE_ID_RE.match(cid):
            label = f"case {cid}"
            if cid in seen:
                errors.append(f"{label}: duplicate id")
            seen.add(cid)
        else:
            label = f"case at index {i}"
            errors.append(f"{label}: id must be a string of 1-64 letters, digits, "
                          f"'_', '.' or '-'")

        missing = [k for k in CASE_KEYS if k not in case]
        if missing:
            errors.append(f"{label}: missing field(s) {', '.join(missing)}")
        extra = len([k for k in case if k not in CASE_KEYS])
        if extra:
            errors.append(f"{label}: {extra} unexpected field(s)")

        for field in ("customer_query", "technician_query"):
            if field not in case:
                continue
            value = case[field]
            if not isinstance(value, str):
                errors.append(f"{label}: {field} must be a string")
            elif not value.strip():
                errors.append(f"{label}: {field} must not be blank")
            elif len(value) > MAX_QUERY_CHARS:
                errors.append(f"{label}: {field} is longer than "
                              f"{MAX_QUERY_CHARS} characters")

        targets_ok = False
        if "target_article_ids" in case:
            targets = case["target_article_ids"]
            if not isinstance(targets, list):
                errors.append(f"{label}: target_article_ids must be a list")
            elif not all(isinstance(t, str) for t in targets):
                errors.append(f"{label}: target_article_ids must contain only strings")
            elif not all(targets):
                errors.append(f"{label}: target_article_ids must not contain empty strings")
            elif len(set(targets)) != len(targets):
                errors.append(f"{label}: target_article_ids has duplicates")
            else:
                targets_ok = True

        has_match_ok = False
        if "has_match" in case:
            if isinstance(case["has_match"], bool):
                has_match_ok = True
            else:
                errors.append(f"{label}: has_match must be true or false")

        if targets_ok and has_match_ok:
            if case["has_match"] and not case["target_article_ids"]:
                errors.append(f"{label}: has_match is true but target_article_ids is empty")
            if not case["has_match"] and case["target_article_ids"]:
                errors.append(f"{label}: has_match is false but target_article_ids "
                              f"is not empty")
    return errors


# ------------------------------------------------------------------ server


def check_base_url(base_url: str, allow_remote: bool) -> str | None:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "--base-url must be an http:// or https:// URL with a host"
    if parts.username or parts.password or parts.query or parts.fragment:
        return "--base-url must not contain credentials, a query or a fragment"
    if not allow_remote and parts.hostname not in LOCAL_HOSTS:
        return ("--base-url host is not local and the eval sends query text to it; "
                "pass --allow-remote to use it")
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect is reported as its HTTP status instead of being followed.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SearchClient:
    """POST /api/search over urllib. No proxies (query text must reach only
    --base-url), no redirects, no retries."""

    def __init__(self, base_url: str, timeout: float = REQUEST_TIMEOUT_S):
        self.endpoint = base_url.rstrip("/") + "/api/search"
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect)

    def search(self, query: str, mode: str) -> object:
        body = json.dumps({"query": query, "mode": mode}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ServerError(describe_status(exc.code)) from None
        except OSError as exc:  # URLError, refused, timeout
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise ServerError(f"cannot reach {self.endpoint} ({type(reason).__name__}); "
                              f"is the app running (npm run dev)?") from None
        if status != 200:
            raise ServerError(describe_status(status))
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServerError("response is not JSON") from None


def describe_status(status: int) -> str:
    if status == 400:
        return "HTTP 400 (request rejected: mode not allowed or invalid request)"
    if status >= 500:
        return f"HTTP {status} (server error: check DATABASE_URL and the database)"
    return f"HTTP {status}"


def check_response(resp: object, mode: str) -> list[dict]:
    if not isinstance(resp, dict):
        raise ResponseCheckError("response is not a JSON object")
    if resp.get("mode") != mode:
        raise ResponseCheckError("mode does not match the requested mode")
    results = resp.get("results")
    if not isinstance(results, list):
        raise ResponseCheckError("results is not a list")
    if len(results) > MAX_RESULTS:
        raise ResponseCheckError(f"{len(results)} results, more than {MAX_RESULTS}")
    seen: set[str] = set()
    for pos, r in enumerate(results, start=1):
        if not isinstance(r, dict):
            raise ResponseCheckError(f"result {pos} is not an object")
        rank = r.get("rank")
        if type(rank) is not int:
            raise ResponseCheckError(f"result {pos}: rank is not an integer")
        if rank != pos:
            raise ResponseCheckError(f"result {pos}: rank {rank} out of order "
                                     f"(expected {pos})")
        sid = r.get("sourceArticleId")
        if not isinstance(sid, str):
            raise ResponseCheckError(f"result {pos}: sourceArticleId is not a string")
        if sid in seen:
            raise ResponseCheckError(f"result {pos}: duplicate sourceArticleId")
        seen.add(sid)
        if not isinstance(r.get("title"), str):
            raise ResponseCheckError(f"result {pos}: title is not a string")
        score = r.get("score")
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score)):
            raise ResponseCheckError(f"result {pos}: score is not a finite number")
    return results


# ------------------------------------------------------------------ metrics


def best_rank(results: list[dict], targets: list[str]) -> int | None:
    """Lowest returned rank of any accepted target; None if none was returned.
    Exact string equality; target order does not matter."""
    wanted = set(targets)
    ranks = [r["rank"] for r in results if r["sourceArticleId"] in wanted]
    return min(ranks) if ranks else None


def summarize(ranks: list[int | None]) -> dict:
    n = len(ranks)
    return {
        "positives": n,
        "top1": sum(1 for r in ranks if r == 1),
        "top3": sum(1 for r in ranks if r is not None and r <= 3),
        "mrr_at_50": sum(1 / r for r in ranks if r is not None) / n if n else None,
        "no_target_in_top_50": sum(1 for r in ranks if r is None),
    }


def fmt_count(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else f"{k}/{n} (n/a)"


# ------------------------------------------------------------------ run


def run_cases(cases: list[dict], mode: str, client) -> tuple[dict, int]:
    """One request per case and track, in file order. Returns (tracks, max depth)."""
    tracks = {t: {"field": f, "positives": [], "negatives": []} for t, f in TRACKS}
    observed = 0
    for case in cases:
        cid = case["id"]
        for track, field in TRACKS:
            where = f"case {cid} track {track}"
            try:
                results = check_response(client.search(case[field], mode), mode)
            except ServerError as exc:
                raise EvalError(f"{where}: {exc}", EXIT_SERVER) from None
            except ResponseCheckError as exc:
                raise EvalError(f"response check failed: {where}: {exc}",
                                EXIT_RESPONSE) from None
            observed = max(observed, len(results))
            if case["has_match"]:
                tracks[track]["positives"].append({
                    "case_id": cid,
                    "best_rank": best_rank(results, case["target_article_ids"]),
                    "results": results,
                })
            else:
                tracks[track]["negatives"].append({"case_id": cid, "results": results})
    for t in tracks.values():
        t["metrics"] = summarize([p["best_rank"] for p in t["positives"]])
    return tracks, observed


def print_tracks(tracks: dict, observed: int) -> None:
    print(f"observed depth: max {observed} results per request (limit {MAX_RESULTS})")
    for track, field in TRACKS:
        t = tracks[track]
        m = t["metrics"]
        n = m["positives"]
        mrr = "n/a" if m["mrr_at_50"] is None else f"{m['mrr_at_50']:.3f}"
        print()
        print(f"== Track {track} ({field}) ==")
        print(f"Top-1: {fmt_count(m['top1'], n)}   Top-3: {fmt_count(m['top3'], n)}   "
              f"MRR@50: {mrr} (N={n})")
        print(f"No target in top 50: {m['no_target_in_top_50']} / {n}")
        for p in t["positives"]:
            rank = p["best_rank"]
            where = f"best target rank {rank}" if rank is not None else "not in top 50"
            print(f"  {p['case_id']}  {where}")
        print("Negatives (qualitative review only):")
        if not t["negatives"]:
            print("  (none)")
        for neg in t["negatives"]:
            results = neg["results"]
            if not results:
                print(f"  {neg['case_id']}  no results")
                continue
            top = results[0]
            print(f"  {neg['case_id']}  top: {top['sourceArticleId']} \"{top['title']}\" "
                  f"score {top['score']:.4f}")
            print(f"    top 5: {' | '.join(r['title'] for r in results[:5])}")


def json_report(tracks: dict, observed: int, header: dict) -> dict:
    def slim(results: list[dict]) -> list[dict]:
        return [{"rank": r["rank"], "sourceArticleId": r["sourceArticleId"],
                 "score": r["score"]} for r in results]

    out = dict(header, observed_max_depth=observed, tracks={})
    for track, field in TRACKS:
        t = tracks[track]
        out["tracks"][track] = {
            "field": field,
            "metrics": t["metrics"],
            "positives": [{
                "case_id": p["case_id"],
                "best_rank": p["best_rank"],
                "reciprocal_rank": 1 / p["best_rank"] if p["best_rank"] else 0.0,
                "results": slim(p["results"]),
            } for p in t["positives"]],
            "negatives": [{"case_id": n["case_id"], "results": slim(n["results"])}
                          for n in t["negatives"]],
        }
    return out


def check_json_out(json_out: Path, cases_path: Path) -> str | None:
    if json_out.exists():
        return f"--json-out already exists, refusing to overwrite: {json_out}"
    if json_out.resolve() == cases_path.resolve():
        return "--json-out must not be the cases file"
    if not json_out.parent.is_dir():
        return f"--json-out directory does not exist: {json_out.parent}"
    return None


def run(args: argparse.Namespace, client=None) -> int:
    problem = check_base_url(args.base_url, args.allow_remote)
    if problem is None and args.json_out is not None:
        problem = check_json_out(args.json_out, args.cases)
    if problem:
        raise EvalError(problem, EXIT_USAGE)

    if not args.cases.is_file():
        raise EvalError(f"cases file not found: {args.cases}", EXIT_USAGE)
    raw = args.cases.read_bytes()  # read once; hashed and parsed from these bytes
    digest = hashlib.sha256(raw).hexdigest()
    print(f"cases sha256: {digest}   file: {args.cases}")
    data = parse_cases(raw)
    errors = validate_cases(data)
    if errors:
        for e in errors:
            print(e)
        raise EvalError(f"invalid cases file: {len(errors)} error(s); no requests sent",
                        EXIT_CASES)

    positives = sum(1 for c in data if c["has_match"])
    header = {
        "cases_sha256": digest,
        "mode": args.mode,
        "base_url": args.base_url,
        "rows": {"cases": len(data), "positives": positives,
                 "negatives": len(data) - positives, "requests": len(data) * len(TRACKS)},
    }
    rows = header["rows"]
    print(f"mode: {args.mode}   base url: {args.base_url}")
    print(f"rows: {rows['cases']} cases ({rows['positives']} positive, "
          f"{rows['negatives']} negative), {rows['requests']} requests")

    if client is None:
        client = SearchClient(args.base_url)
    tracks, observed = run_cases(data, args.mode, client)
    print_tracks(tracks, observed)

    if args.json_out is not None:
        report = json_report(tracks, observed, header)
        with open(args.json_out, "x", encoding="utf-8") as f:
            json.dump(report, f, indent=2, allow_nan=False)
            f.write("\n")
        print()
        print(f"wrote {args.json_out}")
    return EXIT_OK


def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", required=True, help="search mode sent to /api/search")
    ap.add_argument("--cases", required=True, type=Path, help="cases JSON file")
    ap.add_argument("--base-url", default="http://127.0.0.1:3000")
    ap.add_argument("--allow-remote", action="store_true",
                    help="allow a --base-url host other than localhost")
    ap.add_argument("--json-out", type=Path, default=None,
                    help="also write results as JSON to this new file")
    args = ap.parse_args(argv)
    try:
        return run(args, client)
    except EvalError as exc:
        print(f"error: {exc}")
        return exc.code
    except Exception as exc:  # message and traceback could carry values
        print(f"unexpected error: {type(exc).__name__}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    # Titles can hold characters the console code page cannot encode.
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
