#!/usr/bin/env python3
"""Copy and resolve one immutable pose-template selection for a run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from posetestbot.pose_templates.selection import select_pose_template


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    value = json.loads(Path(args.request).read_text())
    confirmed = value.get("confirmed", False)
    if type(confirmed) is not bool:
        raise ValueError("confirmed must be a boolean")
    result = select_pose_template(
        value["run_root"],
        value["template_uuid"],
        placement=value["placement"],
        confirmed=confirmed,
        operator=value.get("operator"),
    )
    print(
        json.dumps(
            {
                "template_uuid": result["template_uuid"],
                "instance_count": len(result["instances"]),
                "bundle_sha256": result["bundle_sha256"],
                "placement_confirmed": result["placement_confirmed"],
                "selection_artifact": "pose_template_selection.json",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
