#!/usr/bin/env python
"""Paired, per-case comparison of two run_eval.py --json-out reports.

Run:  .venv/Scripts/python scripts/paired_eval.py BASE NEW

BASE is the comparator (for the hybrid experiment: that session's semantic
run). NEW is the mode under test. The modes may differ; the cases may not.

Checks   Both reports have the same cases_sha256 and the same tracks, and per
         track the same positive case IDs in the same order. Anything else is
         an error: no comparison is printed.
Counts   Per track, on Top-1 and on Top-3. A win is a case NEW has inside the
         cutoff and BASE does not; a loss is the reverse; everything else is a
         tie. best_rank is read from the reports as run_eval.py wrote it; this
         script does no ranking and reads no result lists.
Rule     The decision rule frozen in DECISIONS.md ("Hybrid experiment
         (hybrid-rrf-v1)", 2026-11-15), stated before any hybrid run:
         a signal is real only with at least 6 wins and 0 losses, or at least
         8 wins and at most 1 loss (two-sided sign test at 5%). The primary
         comparison is Track B Top-1. On Track A, a net loss of 4 or more on
         Top-1 or Top-3, or a target BASE returned that NEW does not, is a
         regression. The exact two-sided sign-test p is printed beside each
         count.
Output   Case IDs, ranks and counts only. The reports hold no query text or
         titles, and the base URL is not printed.
Exit     0 comparison printed, 2 usage, unreadable report or reports that
         cannot be paired. The exit code does not encode the result.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from compare_eval_json import ReportError, load_report

CUTOFFS = (("Top-1", 1), ("Top-3", 3))
PRIMARY = ("B", "Top-1")
REGRESSION_TRACK = "A"
REGRESSION_NET_LOSS = 4

EXIT_OK = 0
EXIT_USAGE = 2


def check_best_ranks(report: dict, label: str) -> None:
    """load_report checks the shape; best_rank is what this script reads."""
    for t, track in report["tracks"].items():
        for case in track["positives"]:
            rank = case.get("best_rank")
            if rank is not None and (type(rank) is not int or rank < 1):
                raise ReportError(f"{label}: {case['case_id']} track {t}: "
                                  f"best_rank is not null or a positive integer")


def check_pairable(base: dict, new: dict) -> None:
    if base["cases_sha256"] != new["cases_sha256"]:
        raise ReportError("reports were made from different cases files "
                          "(cases_sha256 differs)")
    if set(base["tracks"]) != set(new["tracks"]):
        raise ReportError("reports do not have the same tracks")
    for t in sorted(base["tracks"]):
        base_ids = [c["case_id"] for c in base["tracks"][t]["positives"]]
        new_ids = [c["case_id"] for c in new["tracks"][t]["positives"]]
        if base_ids != new_ids:
            raise ReportError(f"track {t}: positives are not the same cases in "
                              f"the same order")


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test. Ties are left out; no wins or losses is 1."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1))
    return min(1.0, 2 * tail / 2 ** n)


def meets_bar(wins: int, losses: int) -> bool:
    return (wins >= 6 and losses == 0) or (wins >= 8 and losses <= 1)


def hit(rank: int | None, cutoff: int) -> bool:
    return rank is not None and rank <= cutoff


def pair_track(base_track: dict, new_track: dict) -> list[tuple[str, int | None, int | None]]:
    return [(b["case_id"], b["best_rank"], n["best_rank"])
            for b, n in zip(base_track["positives"], new_track["positives"])]


def count(pairs: list, cutoff: int) -> dict:
    wins = [p for p in pairs if hit(p[2], cutoff) and not hit(p[1], cutoff)]
    losses = [p for p in pairs if hit(p[1], cutoff) and not hit(p[2], cutoff)]
    return {"wins": wins, "losses": losses,
            "ties": len(pairs) - len(wins) - len(losses)}


def fmt_rank(rank: int | None) -> str:
    return "none" if rank is None else str(rank)


def compare(base: dict, new: dict) -> list[str]:
    lines = [f"base: mode {base['mode']}   new: mode {new['mode']}",
             f"cases sha256: {base['cases_sha256']}",
             "bar: at least 6 wins and 0 losses, or at least 8 wins and at most "
             "1 loss; a net change of 1 to 3 cases is noise"]
    for t in sorted(base["tracks"]):
        pairs = pair_track(base["tracks"][t], new["tracks"][t])
        field = base["tracks"][t].get("field")
        name = f"Track {t}" + (f" ({field})" if isinstance(field, str) else "")
        lines.append("")
        lines.append(f"== {name}, {len(pairs)} positives ==")
        regression = []
        for label, cutoff in CUTOFFS:
            c = count(pairs, cutoff)
            w, l = len(c["wins"]), len(c["losses"])
            role = "primary" if (t, label) == PRIMARY else "secondary"
            lines.append(
                f"{label} ({role}): wins {w}, losses {l}, ties {c['ties']}, "
                f"net {w - l:+d}   sign test p = {sign_test_p(w, l):.3f}   "
                f"bar: {'met' if meets_bar(w, l) else 'not met'}")
            for kind, cases in (("win ", c["wins"]), ("loss", c["losses"])):
                for cid, old, cur in cases:
                    lines.append(f"  {kind}  {cid}  rank {fmt_rank(old)} -> {fmt_rank(cur)}")
            if l - w >= REGRESSION_NET_LOSS:
                regression.append(f"{label} net loss {l - w}")
        better = sum(1 for _, o, n in pairs if n is not None and (o is None or n < o))
        worse = sum(1 for _, o, n in pairs if o is not None and (n is None or n > o))
        lines.append(f"Rank direction (descriptive only): better {better}, "
                     f"worse {worse}, same {len(pairs) - better - worse}")
        if t == REGRESSION_TRACK:
            dropped = [cid for cid, o, n in pairs if o is not None and n is None]
            if dropped:
                regression.append("left the top 50: " + ", ".join(dropped))
            lines.append("Regression check (net loss of 4 or more, or a target "
                         "leaving the top 50): "
                         + ("REGRESSION: " + "; ".join(regression) if regression else "none"))
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base", type=Path, help="--json-out report of the comparator mode")
    ap.add_argument("new", type=Path, help="--json-out report of the mode under test")
    args = ap.parse_args(argv)
    try:
        base = load_report(args.base, "base")
        new = load_report(args.new, "new")
        check_best_ranks(base, "base")
        check_best_ranks(new, "new")
        check_pairable(base, new)
    except ReportError as exc:
        print(f"error: {exc}")
        return EXIT_USAGE
    for line in compare(base, new):
        print(line)
    return EXIT_OK


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
