"""
The CLI's consoles, and the thread that writes their output for others.

`console` writes to stdout, and `error_console` to stderr, which the log,
Cyclopts and the live display write to too.

A terminal can stop taking output: a selection in a Windows console window,
Ctrl+S in a POSIX terminal, a pager that stops reading. Written from the event
loop, that would freeze the whole tunnel. So what mustn't wait for the terminal is handed to the output thread (`write_later`), which writes it
in the order it was handed over: the log's records (see `ezhpcy.logging`), and the
live display. Another thread waits for it only to flush (`flush_output`), and it
waits for no other thread, so the two never wait for each other.

A print from another thread, such as a prompt's, flushes first, so that it comes
after what was handed over before it. Such a print writes to the terminal
directly, so don't print from the event loop while the tunnel serves: log
instead, or print from a thread (`asyncio.to_thread`).

A live display (`live_display`) stays at the bottom of the terminal, under the
log, on `error_console`. The output thread draws it, and redraws it a few times a
second. Prompts take it away while they wait (`live_display_paused`).
"""

import threading
from contextlib import contextmanager, suppress
from typing import Any
lazy import atexit
lazy import functools
lazy import queue
lazy import sys
lazy import time
lazy import traceback
lazy from collections.abc import Callable, Generator

from rich.console import Console
lazy from rich.console import RenderableType
lazy from rich.live import Live

_REDRAW_SECONDS = 0.25


class _Console(Console):
    def print(self, *objects: Any, **kwargs: Any) -> None:
        flush_output()
        super().print(*objects, **kwargs)


def make_console(**options: Any) -> Console:
    """
    A console like the CLI's, with Rich's `Console` options. Only the parts that
    a message marks are styled (see `ezhpcy.messages`).
    """
    return _Console(highlight=False, **options)


console = make_console()
error_console = make_console(stderr=True)


class _OutputThread(threading.Thread):
    """
    Calls what it's handed, in order, and between those, redraws the live display
    it shows, if any, when due. Only it touches that display.
    """

    def __init__(self) -> None:
        super().__init__(name="output", daemon=True)
        self.writes: queue.SimpleQueue[Callable[[], object]] = queue.SimpleQueue()
        self._live: Live | None = None
        # One that failed to draw: drawn again, it would fail again, a traceback
        # each time, so it is never shown again, its error reported once.
        self._failed: Live | None = None
        self._next_redraw = 0.0

    def run(self) -> None:
        while True:
            timeout = None
            if self._live is not None:
                timeout = max(0.0, self._next_redraw - time.monotonic())
            try:
                write = self.writes.get(timeout=timeout)
            except queue.Empty:
                pass
            else:
                _call(write)
            if self._live is not None and time.monotonic() >= self._next_redraw:
                self._redraw(self._live)

    def _redraw(self, live: Live) -> None:
        if _call(live.refresh):
            self._next_redraw = time.monotonic() + _REDRAW_SECONDS
            return
        self._live, self._failed = None, live
        with suppress(Exception):
            live.stop()

    def show(self, live: Live) -> None:
        if self._live is not None or live is self._failed:
            return
        # Shown only if it can be drawn; if not, `Live` stops it.
        if not _call(functools.partial(live.start, refresh=True)):
            self._failed = live
            return
        self._live = live
        self._next_redraw = time.monotonic() + _REDRAW_SECONDS

    def hide(self, live: Live) -> None:
        if self._live is not live:
            return
        self._live = None
        live.stop()


def _call(write: Callable[[], object]) -> bool:
    """Call `write`, and return whether it succeeded; if not, print why."""
    try:
        write()
    # ruff: ignore[BLE001]  # Raised, it would end the thread; flushes would hang
    except Exception:
        traceback.print_exc(file=sys.__stderr__)
        return False
    return True


_output: _OutputThread | None = None
_output_started = threading.Lock()


def _output_thread() -> _OutputThread:
    global _output
    with _output_started:
        if _output is None:
            _output = _OutputThread()
            _output.start()
            # A daemon thread: write what is left before exiting.
            atexit.register(flush_output)
    return _output


def write_later(write: Callable[[], object]) -> None:
    """
    Have the output thread call `write`, after what was handed to it before, and
    return at once. `write` mustn't wait for any other thread.
    """
    _output_thread().writes.put_nowait(write)


def flush_output(timeout: float | None = None) -> bool:
    """
    Wait until what was handed to the output thread so far is written, for at most
    `timeout` seconds, and return whether it was. Blocks while the terminal doesn't
    take output, so call it from the event loop only where that may block it, e.g.
    before a prompt. From the output thread itself, returns at once.
    """
    if _output is None or threading.current_thread() is _output:
        return True
    written = threading.Event()
    write_later(written.set)
    return written.wait(timeout)


# The live display shown, if any. The output thread draws it.
_live: Live | None = None


@contextmanager
def live_display(get_renderable: Callable[[], RenderableType]) -> Generator[bool]:
    """
    Within the block, show what `get_renderable` returns at the bottom of the
    terminal, under the log, and redraw it a few times a second. Yields whether
    it is shown: not unless `error_console` is a terminal that can redraw it, nor
    within another live display.

    `get_renderable` is called once as the display is made, then from the output
    thread: anything it reads may change meanwhile. Showing and hiding the display
    are handed to the output thread too, so neither waits for the terminal. If it
    fails to draw, its error is printed once, and the display hidden.
    """
    global _live
    if (
        _live is not None
        or not error_console.is_terminal
        or error_console.is_dumb_terminal
    ):
        yield False
        return
    # Transient, so that hidden, it leaves the terminal as it found it.
    live = _live = Live(
        console=error_console,
        auto_refresh=False,
        transient=True,
        get_renderable=get_renderable,
    )
    output = _output_thread()
    write_later(functools.partial(output.show, live))
    try:
        yield True
    finally:
        _live = None
        write_later(functools.partial(output.hide, live))


@contextmanager
def live_display_paused() -> Generator[None]:
    """
    Hide the live display, if one is shown, within the block, such as while a
    prompt waits for the user: redrawn, it would write over what they type. Waits
    for it to be hidden.
    """
    live = _live
    if live is None:
        yield
        return
    output = _output_thread()
    write_later(functools.partial(output.hide, live))
    flush_output()
    try:
        yield
    finally:
        write_later(functools.partial(output.show, live))


def hide_live_display() -> None:
    """
    Hide the live display, if one is shown, before quitting at once, so that the
    terminal gets its cursor back once the output is flushed.
    """
    if _live is not None:
        write_later(functools.partial(_output_thread().hide, _live))
