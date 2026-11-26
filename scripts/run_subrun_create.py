#!/usr/bin/env python3
"""Create an object-dataset subrun that reuses a parent run's setup."""

from __future__ import annotations

import argparse
import json

from posetestbot.jobs.cancellation import raise_on_first_cancel_signal
from posetestbot.pipeline.subrun import create_subrun


class SubrunCanceled(Exception):
    """Cancellation requested; create_subrun removes its partial folder."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True, help="Parent dataset run folder.")
    parser.add_argument("--folder-name", required=True, help="New sibling folder name.")
    parser.add_argument("--variant", required=True, help="What differs in this subrun.")
    args = parser.parse_args()
    raise_on_first_cancel_signal(SubrunCanceled("Subrun creation canceled"))
    result = create_subrun(args.parent, args.folder_name, args.variant)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
