"""Run the eval suite.

    python -m evals                     everything
    python -m evals --fast              the subset CI runs on every push
    python -m evals --category consent
    python -m evals --case consent-hedge
    python -m evals --verbose           print full transcripts

Exit code is 1 if any case fails, so CI can gate on it.
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv("mayday/.env")

from evals.cases import select  # noqa: E402
from evals.runner import EVAL_MODEL, run_suite  # noqa: E402

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--category", default="")
    parser.add_argument("--case", default="")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--pause",
        type=float,
        default=20.0,
        help="seconds between cases; free-tier quota is per minute",
    )
    args = parser.parse_args()

    cases = select(fast=args.fast, category=args.category, case_id=args.case)
    if not cases:
        print("no cases matched")
        return 1

    print(f"{DIM}model={EVAL_MODEL}  cases={len(cases)}  pause={args.pause}s{OFF}\n")
    results = asyncio.run(run_suite(cases, pause=args.pause))

    failed = errored = 0
    for r in results:
        if r.trace.error:
            errored += 1
            mark, colour = "ERROR", YELLOW
        elif r.passed:
            mark, colour = "PASS ", GREEN
        else:
            failed += 1
            mark, colour = "FAIL ", RED

        if r.score is None:
            score = ""
        elif r.score == 0:
            score = f"  {YELLOW}judge n/a{OFF}"
        else:
            score = f"  judge {r.score}/5"
        print(f"{colour}{mark}{OFF} {r.case.id:24} {r.case.category:12} {r.seconds:5.1f}s{score}")

        if r.trace.error:
            print(f"       {YELLOW}{r.trace.error}{OFF}")
        for failure in r.failures:
            print(f"       {RED}- {failure}{OFF}")
        if r.score == 0:
            print(f"       {YELLOW}- {r.score_reason}{OFF}")
        elif r.score and r.score < r.case.min_score:
            print(f"       {YELLOW}- quality below bar: {r.score_reason}{OFF}")
        if args.verbose:
            print(f"{DIM}{r.trace.transcript()}{OFF}\n")

    passed = len(results) - failed - errored
    print(f"\n{passed} passed, {failed} failed, {errored} errored")
    # An errored case never got a fair run, so it is not a pass either.
    return 1 if (failed or errored) else 0


if __name__ == "__main__":
    sys.exit(main())
