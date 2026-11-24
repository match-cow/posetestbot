"""Cancellation signal handling shared by cancelable job workers."""

from __future__ import annotations

import signal
from collections.abc import Callable


def write_terminal_progress(
    progress: Callable[..., None], error: BaseException, cancel_type: type[BaseException],
    *, fallback: str = "Job interrupted",
) -> None:
    try:
        progress("canceled" if isinstance(error, cancel_type) else "failed", error=str(error) or fallback)
    except cancel_type as cancellation:
        progress("canceled", error=str(cancellation) or fallback)
        raise


def raise_on_first_cancel_signal(exception: BaseException) -> None:
    """Raise ``exception`` in the main thread on the first SIGTERM or SIGINT.

    Later signals are ignored. ``LocalJobRunner`` signals the whole workload
    process group and ``uv run`` forwards the same signal to its child, so a
    worker normally receives one cancellation twice. A second raise would land
    inside the handler that is recording the terminal ``canceled`` state and
    leave the job's progress file at its last working state.
    """

    def cancel(_signum: int, _frame: object) -> None:
        for handled in (signal.SIGTERM, signal.SIGINT):
            signal.signal(handled, signal.SIG_IGN)
        raise exception

    for handled in (signal.SIGTERM, signal.SIGINT):
        signal.signal(handled, cancel)
