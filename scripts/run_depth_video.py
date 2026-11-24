"""Local depth quality video worker. Reads completed evidence; no hardware access."""

import argparse

from posetestbot.bop.depth_evaluation import DepthCanceled
from posetestbot.bop.depth_videos import run_video
from posetestbot.jobs.cancellation import raise_on_first_cancel_signal


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    raise_on_first_cancel_signal(DepthCanceled("Depth video canceled"))
    run_video(args.request)
