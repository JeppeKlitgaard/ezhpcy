"""
Interactive prompts from async commands.

A prompt blocks the event loop while it waits, which is harmless where nothing
else runs on it: while connecting, or before a command starts its background
work. Call `ask` only there.
"""

lazy import signal
lazy from collections.abc import Callable


def ask[**P, T](prompt: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """
    Run a blocking prompt, such as Rich's `Prompt.ask`, and return its answer.

    Ctrl+C (and Ctrl+Break, on Windows) raises `KeyboardInterrupt` at the prompt,
    as in a synchronous program. Under `asyncio.run`, the first Ctrl+C would
    otherwise only cancel the main task, which can't happen while the prompt
    blocks the loop, so the prompt would keep waiting; so would a handler like
    `tunnel`'s, which stops the tunnel through the loop. Must be called from the
    main thread.
    """
    signals = [signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    previous = {
        signum: signal.signal(signum, signal.default_int_handler) for signum in signals
    }
    try:
        return prompt(*args, **kwargs)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
