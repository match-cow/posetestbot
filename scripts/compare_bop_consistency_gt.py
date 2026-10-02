"""Create an offline Inspect comparison from completed run-scoped evaluations."""

from __future__ import annotations

import argparse
from pathlib import Path

from posetestbot.bop.consistency_comparison import create_comparison


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evaluation",
        action="append",
        nargs=3,
        required=True,
        metavar=("LABEL", "RUN", "EVALUATION_ID"),
        help="First condition is the baseline; repeat for each condition.",
    )
    parser.add_argument("--output-run", type=Path, required=True)
    parser.add_argument("--title", default="Robot consistency vs ground truth")
    parser.add_argument("--reference-frame", type=int)
    args = parser.parse_args()
    print(
        create_comparison(
            [
                (label, Path(root), identifier)
                for label, root, identifier in args.evaluation
            ],
            output_run=args.output_run,
            title=args.title,
            reference_frame=args.reference_frame,
        )
    )


if __name__ == "__main__":
    main()
