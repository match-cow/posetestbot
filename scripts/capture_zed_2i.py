#!/usr/bin/env python3
"""Capture aligned RGB-D frames from a Stereolabs ZED 2i."""

from __future__ import annotations

import argparse
import signal
import threading

from posetestbot.sensors.zed_2i import (
    ZED2iCaptureError,
    capture_zed_2i_rgbd,
    summary_to_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ZED 2i RGB-D capture")
    parser.add_argument("output_path", help="Output folder for recording.")
    parser.add_argument("--test", action="store_true", help="Preview without recording.")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--max_frames", type=int, default=0)
    parser.add_argument("--warmup-frames", type=int, default=0)
    parser.add_argument("--device", default=None, help="ZED numeric serial number.")
    parser.add_argument("--resolution", choices=("720p", "360p"), default="720p")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stop_requested = threading.Event()
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_graceful_stop(_signum, _frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_graceful_stop)
    try:
        try:
            summary = capture_zed_2i_rgbd(
                args.output_path,
                device_id=args.device,
                fps=args.fps,
                max_frames=args.max_frames,
                warmup_frames=args.warmup_frames,
                resolution=args.resolution,
                preview=args.preview,
                record=not args.test,
                stop_requested=stop_requested.is_set,
            )
        except (ValueError, ZED2iCaptureError) as exc:
            raise SystemExit(str(exc)) from exc
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    print(
        "ZED 2i capture: "
        f"{summary['status']} {summary['sensor_id']} "
        f"frames={summary['frame_count']} preview={summary['preview']}"
    )
    if args.print_json:
        print(summary_to_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
