"""Render one retained Pose Results visualization through LocalJobRunner."""

import argparse
import json
import signal

from posetestbot.bop.inspection_exports import ExportCanceled, run_export_request


def cancel(_signum, _frame):
    raise ExportCanceled("Visualization export canceled")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    print(json.dumps(run_export_request(args.request), indent=2))


if __name__ == "__main__":
    main()
