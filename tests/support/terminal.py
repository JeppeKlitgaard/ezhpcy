"""The log's handlers, and terminals that are slow to take its output."""

import io
import logging
import threading
import time
from pathlib import Path
from typing import TextIO

import pytest
from rich.logging import RichHandler

from ezhpcy import console as consoles
from ezhpcy.console import make_console
from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.logging import (
    FILE_HANDLER_NAME,
    OUTPUT_HANDLER_NAME,
    TERMINAL_HANDLER_NAME,
)

# Longest a write waits for the terminal, so that a failing test still ends.
_WAIT_SECONDS = 5


def log_handlers() -> dict[str, logging.Handler]:
    """The handlers that the output thread writes the records with, by name."""
    [output_handler] = [
        handler
        for handler in logging.getLogger(PACKAGE_NAME).handlers
        if handler.get_name() == OUTPUT_HANDLER_NAME
    ]
    handlers = getattr(output_handler, "handlers", None)
    assert isinstance(handlers, tuple)
    return {handler.get_name() or "": handler for handler in handlers}


def terminal_handler() -> RichHandler:
    """
    The handler that writes the queued records to the terminal, behind the one
    that keeps the records to show before an error.
    """
    handler = getattr(log_handlers()[TERMINAL_HANDLER_NAME], "target", None)
    assert isinstance(handler, RichHandler)
    return handler


def use_terminal(monkeypatch: pytest.MonkeyPatch, stream: TextIO) -> None:
    """
    Write the log, and show live displays, on `stream` as on a terminal: one that
    takes cursor movements, but no colour, so that what it shows can be read off.
    """
    terminal = make_console(
        file=stream,
        force_terminal=True,
        color_system=None,
        width=120,
        legacy_windows=False,
    )
    monkeypatch.setattr(consoles, "error_console", terminal)
    monkeypatch.setattr(terminal_handler(), "console", terminal)


def file_handler() -> logging.FileHandler | None:
    """The handler that writes the queued records to the log file, if any."""
    handler = log_handlers().get(FILE_HANDLER_NAME)
    assert handler is None or isinstance(handler, logging.FileHandler)
    return handler


class GatedTerminal(io.StringIO):
    """
    Takes no output until `opened` is set, as a Windows console window with a
    selection in it, a POSIX terminal after Ctrl+S, or a pager that stopped reading.
    """

    def __init__(self) -> None:
        super().__init__()
        self.opened = threading.Event()

    def write(self, text: str) -> int:
        self.opened.wait(_WAIT_SECONDS)
        return super().write(text)


class SlowLogTerminal(io.StringIO):
    """
    Takes a while to take each log record, written from the output thread, but
    takes the main thread's output at once. Without a flush, that output would
    come before records logged earlier.
    """

    def write(self, text: str) -> int:
        if threading.current_thread() is not threading.main_thread():
            time.sleep(0.2)
        return super().write(text)


def log_file_path() -> Path:
    """Where the log file is written, or will be once a record comes."""
    handler = file_handler()
    assert handler is not None
    return Path(handler.baseFilename)
