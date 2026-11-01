"""
Single keys from the terminal, beside the live display (`read_keys`), so that a
status panel can take them while the event loop keeps running and the log keeps
scrolling above it. blessed reads and decodes them. It also asks the terminal what
it can do first: one that doesn't answer takes it seconds, so that is done on a
thread, and keys aren't read until then.

Ctrl+C stays a signal, so that what handles it, and the second Ctrl+C that
force-quits, work as without this, even with the event loop blocked: cbreak mode
keeps the terminal's signals. Resuming after Ctrl+Z sets it again, as bash's `fg`
leaves the terminal in its normal mode.

Printable keys come as themselves, and `ENTER`, `BACKSPACE` and `ESCAPE` as those.
Other keys, such as arrows, are dropped.
"""

import sys
from contextlib import asynccontextmanager
lazy import asyncio
lazy import functools
lazy import os
lazy import signal
lazy from collections.abc import AsyncGenerator, Callable
lazy from types import FrameType

lazy import blessed
lazy from blessed.keyboard import Keystroke

ENTER = "\r"
BACKSPACE = "\x7f"
ESCAPE = "\x1b"
_NAMED_KEYS = {
    "KEY_ENTER": ENTER,
    "KEY_BACKSPACE": BACKSPACE,
    "KEY_ESCAPE": ESCAPE,
}
_INTERRUPT = "KEY_CTRL_C"
# What gives the terminal back its mode, while keys are read.
_restore: Callable[[], object] | None = None


def key_of(keystroke: Keystroke) -> str | None:
    """A keystroke as this module gives it, if it gives it."""
    if keystroke.name is not None:
        return _NAMED_KEYS.get(keystroke.name)
    text = str(keystroke)
    return text if text.isprintable() else None


def _make_terminal() -> blessed.Terminal:
    # On stderr, as the live display is: stdout may be a pipe, for scripts, and
    # blessed reads the keyboard only for a stream that is a terminal.
    return blessed.Terminal(stream=sys.__stderr__)


def _enter_cbreak(terminal: blessed.Terminal) -> None:
    """
    Enter cbreak mode, by hand rather than with `with`, so that quitting at once can
    leave it too (`restore_terminal`). Left first if it was entered, as what it
    saved would be restored whenever that generator is collected.
    """
    global _restore
    restore_terminal()
    cbreak = terminal.cbreak()
    # ruff: ignore[PLC2801]  # It must outlast this function: see the docstring
    cbreak.__enter__()
    _restore = functools.partial(cbreak.__exit__, None, None, None)


async def _read(on_key: Callable[[str], object]) -> None:
    terminal = await asyncio.to_thread(_make_terminal)
    _enter_cbreak(terminal)
    if sys.platform != "win32":
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGCONT, _enter_cbreak, terminal
        )
    while True:
        keystroke = await terminal.async_inkey()
        if keystroke.name == _INTERRUPT:
            # On Windows, in the moment it reads, Ctrl+C is a key.
            signal.raise_signal(signal.SIGINT)
        elif (key := key_of(keystroke)) is not None:
            on_key(key)


def _terminated(signum: int, _frame: FrameType | None) -> None:
    restore_terminal()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


@asynccontextmanager
async def read_keys(on_key: Callable[[str], object]) -> AsyncGenerator[bool]:
    """
    Within the block, call `on_key` for each key pressed (see the module). Yields
    whether keys are read: not unless stdin is a terminal, and on POSIX, one this
    process has in the foreground, as changing its mode from the background would
    stop the process.
    """
    global _restore
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        yield False
        return
    if sys.platform != "win32" and os.tcgetpgrp(stdin.fileno()) != os.getpgrp():
        yield False
        return
    previous_term = signal.signal(signal.SIGTERM, _terminated)
    reading = asyncio.create_task(_read(on_key), name="keys")
    try:
        yield True
    finally:
        reading.cancel()
        await asyncio.wait([reading])
        if sys.platform != "win32":
            asyncio.get_running_loop().remove_signal_handler(signal.SIGCONT)
        signal.signal(signal.SIGTERM, previous_term)
        restore_terminal()
        _restore = None


def restore_terminal() -> None:
    """Undo what reading keys did to the terminal, before quitting at once."""
    if _restore is not None:
        _restore()
