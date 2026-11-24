#!/usr/bin/env python3
"""Background inventory, move, and deletion worker for web-managed run folders."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from posetestbot.run_folders import (
    delete_run_folder,
    move_run_folder,
    write_run_folder_inventory,
)
from posetestbot.web.security import web_run_roots


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)

    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("--cache", required=True)

    move = subparsers.add_parser("move")
    move.add_argument("run_root")
    move.add_argument("destination_root")
    move.add_argument("--expected-device", required=True, type=int)
    move.add_argument("--expected-inode", required=True, type=int)
    move.add_argument("--expected-destination-device", required=True, type=int)
    move.add_argument("--expected-destination-inode", required=True, type=int)
    move.add_argument("--cache", required=True)

    delete = subparsers.add_parser("delete")
    delete.add_argument("run_root")
    delete.add_argument("--expected-device", required=True, type=int)
    delete.add_argument("--expected-inode", required=True, type=int)
    delete.add_argument("--cache", required=True)
    delete.add_argument(
        "--confirm-delete",
        action="store_true",
        required=True,
        help="Required acknowledgement for permanent run-folder deletion.",
    )
    calibration = delete.add_mutually_exclusive_group()
    calibration.add_argument(
        "--retain-calibration",
        dest="retain_calibration",
        action="store_const",
        const=True,
        help="Keep the run's promoted calibration in the calibration library.",
    )
    calibration.add_argument(
        "--discard-calibration",
        dest="retain_calibration",
        action="store_const",
        const=False,
        help="Delete the run's promoted calibration with the run.",
    )
    return parser


def _refresh(cache: str | Path) -> dict:
    return write_run_folder_inventory(cache, allowed_roots=web_run_roots())


def _invalidate(cache: str | Path) -> str | None:
    try:
        Path(cache).unlink(missing_ok=True)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def main() -> None:
    args = _parser().parse_args()
    if args.operation == "inventory":
        result = _refresh(args.cache)
    elif args.operation == "move":
        result = move_run_folder(
            args.run_root,
            args.destination_root,
            expected_identity={
                "device": args.expected_device,
                "inode": args.expected_inode,
            },
            expected_destination_root_identity={
                "device": args.expected_destination_device,
                "inode": args.expected_destination_inode,
            },
            allowed_roots=web_run_roots(),
        )
        warning = _invalidate(args.cache)
        result["inventory_cache_invalidated"] = warning is None
        if warning is not None:
            result["inventory_cache_warning"] = warning
    else:
        result = delete_run_folder(
            args.run_root,
            expected_identity={
                "device": args.expected_device,
                "inode": args.expected_inode,
            },
            allowed_roots=web_run_roots(),
            retain_calibration=args.retain_calibration,
        )
        warning = _invalidate(args.cache)
        result["inventory_cache_invalidated"] = warning is None
        if warning is not None:
            result["inventory_cache_warning"] = warning
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
