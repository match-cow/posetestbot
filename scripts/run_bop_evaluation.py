"""Run one queued Inspect-only BOP evaluation request."""

from __future__ import annotations

import argparse
import json

from posetestbot.bop.evaluation import EvaluationCanceled, run_evaluation_request
from posetestbot.jobs.cancellation import raise_on_first_cancel_signal
from posetestbot.web.paths import APP_ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise_on_first_cancel_signal(EvaluationCanceled("BOP evaluation canceled"))
    report = run_evaluation_request(args.request, app_root=APP_ROOT)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
