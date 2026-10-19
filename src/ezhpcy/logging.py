"""
EzHPCy's terminal log.

Records go through a queue to a thread that formats and writes them, so that
logging never blocks the event loop. A terminal can stop taking output: a
selection in a Windows console window, Ctrl+S in a POSIX terminal, a pager that
stops reading. Written from the loop, that froze the whole tunnel (FINDINGS.md,
"Phase 4").

Output written straight to the terminal, such as a prompt, calls `flush_logs`
first, so that it comes after the records logged before it.
"""

import logging
import threading
from logging.handlers import QueueHandler, QueueListener
lazy import atexit
lazy import copy
lazy import queue
lazy from typing import TextIO

lazy from rich.console import Console
lazy from rich.logging import RichHandler

lazy from ezhpcy.constants import PACKAGE_NAME

TERMINAL_HANDLER_NAME = "ezhpcy-terminal"
LOG_QUEUE_HANDLER_NAME = "ezhpcy-log-queue"
LOG_FORMAT = "[%(name)s] %(message)s"
ASYNCSSH_LOGGER_NAME = "asyncssh"

# Set up by the first `configure_logging`, and kept for the process.
_terminal_handler: RichHandler | None = None
_queue_handler: QueueHandler | None = None
_listener: _Listener | None = None


class _Flushed:
    """Queued after the records to flush; set once the listener gets to it."""

    def __init__(self) -> None:
        self.event = threading.Event()


class _Listener(QueueListener):
    def handle(self, record: logging.LogRecord | _Flushed) -> None:
        _in_listener.active = True
        if isinstance(record, _Flushed):
            record.event.set()
        else:
            super().handle(record)


# Set in the listener's thread, where a flush would wait for itself.
_in_listener = threading.local()


class _QueueHandler(QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """
        Resolve the message now, as its arguments may change before the listener
        formats it. The default would also format it, which the terminal handler
        does, and drop the exception, which Rich renders as a traceback.
        """
        record = copy.copy(record)
        record.msg = record.getMessage()
        record.args = None
        return record


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: TextIO | None = None,
    include_timestamp: bool = False,
) -> logging.Logger:
    """Configure EzHPCy's default terminal logger and return it.

    Repeated calls reuse the same handlers, allowing entry points and tests to
    adjust the level without duplicating each emitted record.

    At DEBUG, asyncssh's INFO records go to the same handler.

    The records logged before the call are written first, with the previous
    stream and format.
    """
    global _terminal_handler, _queue_handler, _listener
    flush_logs()
    logger = logging.getLogger(PACKAGE_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if _terminal_handler is None:
        _terminal_handler = RichHandler(
            console=Console(file=stream, highlight=False)
            if stream is not None
            else Console(stderr=True, highlight=False),
            show_time=False,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        _terminal_handler.set_name(TERMINAL_HANDLER_NAME)
    elif stream is not None:
        _terminal_handler.console = Console(file=stream, highlight=False)
    _terminal_handler.setFormatter(
        logging.Formatter(
            f"%(asctime)s {LOG_FORMAT}" if include_timestamp else LOG_FORMAT,
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    if _queue_handler is None:
        records: queue.SimpleQueue[logging.LogRecord | _Flushed] = queue.SimpleQueue()
        _queue_handler = _QueueHandler(records)
        _queue_handler.set_name(LOG_QUEUE_HANDLER_NAME)
        _listener = _Listener(records, _terminal_handler)
        # As `logging.config.dictConfig` sets it, so the terminal handler can be
        # found from the logger.
        _queue_handler.listener = _listener
        _listener.start()
        # The listener's thread is a daemon: write what is left before exiting.
        atexit.register(flush_logs)
    if _queue_handler not in logger.handlers:
        logger.addHandler(_queue_handler)

    # Redirect asyncssh logs.
    # asyncssh never logs above INFO, so it is only shown with debug logging, and
    # only at INFO (connections, authentication, channels): its own debug
    # records are packet-level detail.
    asyncssh_logger = logging.getLogger(ASYNCSSH_LOGGER_NAME)
    asyncssh_logger.removeHandler(_queue_handler)
    if logger.isEnabledFor(logging.DEBUG):
        asyncssh_logger.setLevel(logging.INFO)
        asyncssh_logger.addHandler(_queue_handler)
    else:
        asyncssh_logger.setLevel(logging.NOTSET)

    return logger


def flush_logs(timeout: float | None = None) -> bool:
    """
    Wait until the records logged so far are written, for at most `timeout`
    seconds, and return whether they were. Blocks while the terminal doesn't take
    output, so call it from the event loop only where that may block it, e.g.
    before a prompt.
    """
    if _listener is None or getattr(_in_listener, "active", False):
        return True
    flushed = _Flushed()
    _listener.queue.put_nowait(flushed)
    return flushed.event.wait(timeout)
