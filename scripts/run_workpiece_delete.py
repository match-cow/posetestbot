#!/usr/bin/env python3
"""Validate template references, retire a workpiece, and remove its assets."""

import argparse
import json

from posetestbot.pose_templates.catalog import (
    CatalogObjectInUseError,
    delete_catalog_object,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog_uuid")
    parser.add_argument("--confirm-delete", action="store_true", required=True)
    args = parser.parse_args()
    try:
        result = delete_catalog_object(args.catalog_uuid)
    except CatalogObjectInUseError as exc:
        for blocker in exc.blockers:
            print(
                f"{blocker['display_name']} ({blocker['template_uuid']}): {blocker['reason']}"
            )
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result))
    if result["status"] == "deleted_cleanup_pending":
        raise SystemExit(
            "Workpiece retired. Run the same confirmed deletion again to retry file cleanup."
        )


if __name__ == "__main__":
    main()
