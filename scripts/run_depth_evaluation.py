"""Cancelable local depth evaluation/comparison worker; no hardware access."""

import argparse

from posetestbot.bop.depth_evaluation import (
    DepthCanceled,
    run_comparison,
    run_evaluation,
)
from posetestbot.jobs.cancellation import raise_on_first_cancel_signal


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--kind", choices=("evaluations", "comparisons"), required=True)
    args = parser.parse_args()
    raise_on_first_cancel_signal(DepthCanceled("Depth job canceled"))
    (run_evaluation if args.kind == "evaluations" else run_comparison)(args.request)
