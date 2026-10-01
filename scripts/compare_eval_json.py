#!/usr/bin/env python
"""Compare two run_eval.py --json-out reports result by result.

Run:  .venv/Scripts/python scripts/compare_eval_json.py OLD NEW

Checks   Both reports have the same mode, cases_sha256 and tracks. Per track,
         positives and negatives hold the same case IDs in the same order.
         For every case and track the full results list must match element by
         element: rank, sourceArticleId and score (exact float equality), and
         the same length. Aggregate metrics are not compared on their own: they
         follow from the results.
Output   One line per difference, naming the case ID and track, then
         "identical" or "N differences". Article IDs, ranks and scores are
         printed; nothing else from the reports is (the reports hold no query
         text or titles, and the base URL is not printed).
Exit     0 identical, 1 differences, 2 usage or unreadable report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KINDS = ("positives", "negatives")
RESULT_KEYS = ("rank", "sourceArticleId", "score")

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_USAGE = 2


class ReportError(Exception):
    pass


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


def load_report(path: Path, label: str) -> dict:
    """Parse and shape-check one report. Errors never quote file content."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise ReportError(f"{label}: cannot read {path}") from None
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise ReportError(f"{label}: not valid JSON") from None
    if not isinstance(data, dict):
        raise ReportError(f"{label}: top level is not an object")
    for key in ("mode", "cases_sha256"):
        if not isinstance(data.get(key), str):
            raise ReportError(f"{label}: {key} missing or not a string")
    tracks = data.get("tracks")
    if not isinstance(tracks, dict) or not tracks:
        raise ReportError(f"{label}: tracks missing or empty")
    for t, track in tracks.items():
        if not isinstance(track, dict):
            raise ReportError(f"{label}: track {t} is not an object")
        for kind in KINDS:
            cases = track.get(kind)
            if not isinstance(cases, list):
                raise ReportError(f"{label}: track {t} {kind} is not a list")
            seen = set()
            for i, case in enumerate(cases, 1):
                cid = case.get("case_id") if isinstance(case, dict) else None
                if not isinstance(cid, str):
                    raise ReportError(f"{label}: track {t} {kind} entry {i} has no case_id")
                if cid in seen:
                    raise ReportError(f"{label}: track {t} duplicate case {cid}")
                seen.add(cid)
                results = case.get("results")
                if not isinstance(results, list):
                    raise ReportError(f"{label}: {cid} track {t}: results is not a list")
                for pos, r in enumerate(results, 1):
                    if not isinstance(r, dict) or any(k not in r for k in RESULT_KEYS):
                        raise ReportError(f"{label}: {cid} track {t}: result {pos} malformed")
    return data


def _same_value(a, b) -> bool:
    # bool is an int subclass; 1 and True must not compare equal here.
    return type(a) is type(b) and a == b


def compare_results(old: list[dict], new: list[dict]) -> list[str]:
    diffs = []
    if len(old) != len(new):
        diffs.append(f"length {len(old)} -> {len(new)}")
    for pos, (a, b) in enumerate(zip(old, new), 1):
        for key in RESULT_KEYS:
            if not _same_value(a[key], b[key]):
                diffs.append(f"position {pos} {key} {a[key]!r} -> {b[key]!r}")
    return diffs


def compare_reports(old: dict, new: dict) -> list[str]:
    lines = []
    for key in ("mode", "cases_sha256"):
        if old[key] != new[key]:
            lines.append(f"header: {key} {old[key]} -> {new[key]}")
    old_tracks, new_tracks = old["tracks"], new["tracks"]
    for t in sorted(set(old_tracks) - set(new_tracks)):
        lines.append(f"track {t}: missing in new")
    for t in sorted(set(new_tracks) - set(old_tracks)):
        lines.append(f"track {t}: extra in new")
    for t in sorted(set(old_tracks) & set(new_tracks)):
        for kind in KINDS:
            old_cases = {c["case_id"]: c for c in old_tracks[t][kind]}
            new_cases = {c["case_id"]: c for c in new_tracks[t][kind]}
            old_ids, new_ids = list(old_cases), list(new_cases)
            for cid in old_ids:
                if cid not in new_cases:
                    lines.append(f"{cid} track {t}: missing from new {kind}")
            for cid in new_ids:
                if cid not in old_cases:
                    lines.append(f"{cid} track {t}: extra in new {kind}")
            common_old = [c for c in old_ids if c in new_cases]
            common_new = [c for c in new_ids if c in old_cases]
            if common_old != common_new:
                lines.append(f"track {t} {kind}: case order differs")
            for cid in common_old:
                for d in compare_results(old_cases[cid]["results"],
                                         new_cases[cid]["results"]):
                    lines.append(f"{cid} track {t}: {d}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old", type=Path, help="earlier --json-out report")
    ap.add_argument("new", type=Path, help="later --json-out report")
    args = ap.parse_args(argv)
    try:
        old = load_report(args.old, "old")
        new = load_report(args.new, "new")
    except ReportError as exc:
        print(f"error: {exc}")
        return EXIT_USAGE
    lines = compare_reports(old, new)
    for line in lines:
        print(line)
    if lines:
        print(f"{len(lines)} differences")
        return EXIT_DIFFERENT
    print("identical")
    return EXIT_SAME


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
