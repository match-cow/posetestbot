#!/usr/bin/env python3
"""Audit a recorded wrist-camera bundle against one shared stationary grid."""

from __future__ import annotations

import argparse
import json

from posetestbot.calibration.attempts import (
    create_promotion_request,
    promote_calibration_attempt,
)
from posetestbot.calibration.reprojection_refinement import (
    create_reprojection_refinement,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root")
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument(
        "--refinement-id", help="Use an already reviewed retained report"
    )
    parser.add_argument("--promote", action="store_true")
    parser.add_argument(
        "--replace-refinement",
        action="store_true",
        help="Retain and replace an already promoted refinement with a different reviewed report",
    )
    parser.add_argument("--operator")
    args = parser.parse_args()
    if args.replace_refinement and (not args.promote or not args.refinement_id):
        parser.error("--replace-refinement requires --refinement-id and --promote")
    refinement_id = args.refinement_id
    if refinement_id is None:
        result = create_reprojection_refinement(args.run_root, args.attempt_id)
        refinement_id = result["refinement_id"]
        print(
            json.dumps(
                {
                    "refinement_id": refinement_id,
                    "status": result["status"],
                    "checks": result["checks"],
                },
                indent=2,
            ),
            flush=True,
        )
    if args.promote:
        create_promotion_request(
            args.run_root,
            args.attempt_id,
            operator=args.operator,
            reprojection_refinement_id=refinement_id,
            replace_promoted_refinement=args.replace_refinement,
        )
        print(
            json.dumps(
                promote_calibration_attempt(args.run_root, args.attempt_id), indent=2
            )
        )
    elif args.refinement_id is not None:
        parser.error(
            "--refinement-id requires --promote; review its retained report directly"
        )


if __name__ == "__main__":
    main()
