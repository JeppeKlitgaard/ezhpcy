"""Handling Ctrl+C (and Ctrl+Break, on Windows) differently for a while."""

from contextlib import contextmanager
lazy import signal
lazy from collections.abc import Callable, Generator
lazy from types import FrameType


@contextmanager
def interrupts_handled_by(
    handler: Callable[[int, FrameType | None], object],
) -> Generator[None]:
    """
    Within the block, Ctrl+C (SIGINT) and Ctrl+Break (SIGBREAK, on Windows) call
    `handler`; after it, the previous handlers are back. Must be used from the
    main thread.
    """
    signals = [signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    previous = {signum: signal.signal(signum, handler) for signum in signals}
    try:
        yield
    finally:
        for signum, previous_handler in previous.items():
            signal.signal(signum, previous_handler)
