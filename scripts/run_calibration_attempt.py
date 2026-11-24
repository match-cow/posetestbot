#!/usr/bin/env python3
"""Execute or promote one immutable intent-level calibration attempt."""

from __future__ import annotations

import argparse

from posetestbot.calibration.attempts import (
    promote_calibration_attempt,
    run_calibration_attempt,
)
from posetestbot.jobs.cancellation import raise_on_first_cancel_signal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root")
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--requested-at")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raise_on_first_cancel_signal(KeyboardInterrupt("Calibration job canceled"))
    if args.promote:
        result = promote_calibration_attempt(args.run_root, args.attempt_id, requested_at=args.requested_at)
        print(
            f"Promoted calibration attempt {args.attempt_id}: "
            f"{len(result['promoted_profile_ids'])} camera profile(s)."
        )
    else:
        result = run_calibration_attempt(args.run_root, args.attempt_id)
        print(
            f"Completed calibration attempt {args.attempt_id}: "
            f"{result['recommended_camera_count']} recommendation(s)."
        )


if __name__ == "__main__":
    main()
