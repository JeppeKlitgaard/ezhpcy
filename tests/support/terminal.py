"""The log's handlers, and terminals that are slow to take its output."""

import io
import logging
import logging.handlers
import threading
import time
from pathlib import Path

from rich.logging import RichHandler

from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.logging import (
    FILE_HANDLER_NAME,
    LOG_QUEUE_HANDLER_NAME,
    TERMINAL_HANDLER_NAME,
)

# Longest a write waits for the terminal, so that a failing test still ends.
_WAIT_SECONDS = 5


def log_handlers() -> dict[str, logging.Handler]:
    """The handlers that write the queued records, by name."""
    [queue_handler] = [
        handler
        for handler in logging.getLogger(PACKAGE_NAME).handlers
        if handler.get_name() == LOG_QUEUE_HANDLER_NAME
    ]
    assert isinstance(queue_handler, logging.handlers.QueueHandler)
    assert queue_handler.listener is not None
    return {
        handler.get_name() or "": handler for handler in queue_handler.listener.handlers
    }


def terminal_handler() -> RichHandler:
    """
    The handler that writes the queued records to the terminal, behind the one
    that keeps the records to show before an error.
    """
    handler = getattr(log_handlers()[TERMINAL_HANDLER_NAME], "target", None)
    assert isinstance(handler, RichHandler)
    return handler


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
    Takes a while to take each log record, written from the log's thread, but
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
