import argparse
import signal
import sys
import threading

from posetestbot.sensors.oak_d_pro import (
    DEFAULT_RGB_DEPTH_DELTA_NS,
    OAKDProCaptureError,
    capture_oak_d_pro_rgbd,
    summary_to_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Luxonis OAK-D Pro RGB-D capture")
    parser.add_argument(
        "output_path",
        type=str,
        nargs="?",
        help="Specify the output path for recording.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Capture without recording frames.",
    )
    parser.add_argument(
        "--downscaleColor",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=6,
        help="Specify the frames per second for capturing.",
    )
    parser.add_argument(
        "--max_frames",
        type=int,
        default=0,
        help="Specify the maximum number of frames to capture (0 for unlimited).",
    )
    parser.add_argument(
        "--warmup-frames",
        type=int,
        default=0,
        help="Discard this many synchronized RGB-D pairs before writing output.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="The MX ID of the Luxonis device to use.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show an OpenCV RGB preview window while capturing.",
    )
    parser.add_argument(
        "--max-rgb-depth-delta-ms",
        type=float,
        default=DEFAULT_RGB_DEPTH_DELTA_NS / 1_000_000,
        help="Reject synchronized RGB/depth pairs farther apart than this many ms.",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the capture summary JSON after completion.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.test and not args.output_path:
        print(
            "capture_luxonis_720p.py: output_path is required unless --test is set",
            file=sys.stderr,
        )
        return 2

    stop_requested = threading.Event()
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_graceful_stop(_signum, _frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_graceful_stop)
    try:
        try:
            summary = capture_oak_d_pro_rgbd(
                args.output_path,
                device_id=args.device,
                fps=args.fps,
                max_frames=args.max_frames,
                warmup_frames=args.warmup_frames,
                preview=args.preview,
                record=not args.test,
                max_rgb_depth_delta_ns=int(args.max_rgb_depth_delta_ms * 1_000_000),
                stop_requested=stop_requested.is_set,
            )
        except (OAKDProCaptureError, ValueError) as exc:
            print(f"capture_luxonis_720p.py: {exc}", file=sys.stderr)
            return 2
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

    print(
        "OAK-D Pro capture: "
        f"{summary['status']} {summary['sensor_id']} "
        f"frames={summary['frame_count']} rejected_pairs={summary['rejected_pairs']} "
        f"preview={summary['preview']}"
    )
    if args.print_json:
        print(summary_to_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
