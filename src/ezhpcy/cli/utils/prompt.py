"""
Interactive prompts from async commands.

A prompt blocks the event loop while it waits, which is harmless where nothing
else runs on it: while connecting, or before a command connects or starts its
background work. Call `ask` only there. Once connected, nothing would answer the
server's keepalives, which could drop the connection.
"""

lazy import signal
lazy from collections.abc import Callable

lazy from ezhpcy.cli.utils.interrupts import interrupts_handled_by
lazy from ezhpcy.logging import flush_logs


def ask[**P, T](prompt: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """
    Run a blocking prompt, such as Rich's `Prompt.ask`, and return its answer.

    Ctrl+C (and Ctrl+Break, on Windows) raises `KeyboardInterrupt` at the prompt,
    as in a synchronous program. Under `asyncio.run`, the first Ctrl+C would
    otherwise only cancel the main task, which can't happen while the prompt
    blocks the loop, so the prompt would keep waiting; so would a handler like
    `tunnel`'s, which stops the tunnel through the loop. Must be called from the
    main thread.

    The records logged so far are written first, so none lands after the prompt.
    """
    flush_logs()
    with interrupts_handled_by(signal.default_int_handler):
        return prompt(*args, **kwargs)
