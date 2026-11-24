"""Render one retained Pose Results visualization through LocalJobRunner."""

import argparse
import json

from posetestbot.bop.inspection_exports import ExportCanceled, run_export_request
from posetestbot.jobs.cancellation import raise_on_first_cancel_signal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    raise_on_first_cancel_signal(ExportCanceled("Visualization export canceled"))
    print(json.dumps(run_export_request(args.request), indent=2))


if __name__ == "__main__":
    main()
