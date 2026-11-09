"""
EzHPCy's terminal log, and each run's log file.

Records are handed to the output thread (see `ezhpcy.cli._console`), which formats
and writes them, so that logging never waits for the terminal, and the event loop
with it. `ezhpcy.cli._console.flush_output` waits until they are written.

A record whose message is a template (see `ezhpcy.api.frontend.get_logger`) carries it as plain
text, and the terminal log styles its parts.

Each run can also write its log to a file of its own (`configure_log_file`, with
`ezhpcy.api.frontend.LogFile`), at a level of its own. What a log shows is up to the log,
never the code that logs: tracebacks are shown only at DEBUG, and asyncssh's
records by the terminal only with debug logging (`--debug`), and by the file at
DEBUG.

A record logged with `extra=REPLAY_ON_ERROR`, such as a line of an interactive
job's output, is context for a failure: if the terminal doesn't show it, it keeps
the last of them, and shows them before the next ERROR record.
"""

import logging
lazy import copy
lazy import functools
lazy from collections import deque
lazy from pathlib import Path
lazy from string.templatelib import Template
lazy from typing import Any, TextIO

from rich.logging import RichHandler
lazy from rich.text import Text

from ezhpcy.api.frontend import LogFile
lazy from ezhpcy.cli._console import (
    error_console,
    flush_output,
    make_console,
    write_later,
)
lazy from ezhpcy.cli._messages import rich_text

_PACKAGE_LOGGER_NAME = "ezhpcy"
TERMINAL_HANDLER_NAME = "ezhpcy-terminal"
FILE_HANDLER_NAME = "ezhpcy-file"
OUTPUT_HANDLER_NAME = "ezhpcy-output"
LOG_FORMAT = "[%(name)s] %(message)s"
ASYNCSSH_LOGGER_NAME = "asyncssh"
# How many `REPLAY_ON_ERROR` records the terminal keeps: all of an interactive
# job's output while it starts, which echoes the worker script.
REPLAYED_RECORDS = 1000

# Set up by the first `configure_logging` or `configure_log_file`, and kept for
# the process.
_terminal_handler: _TerminalHandler | None = None
_terminal_replay: _Replay | None = None
_terminal_enabled = True
_output_handler: _OutputHandler | None = None
# Replaced by each `configure_log_file`.
_file_handler: _FileHandler | None = None


class _OutputHandler(logging.Handler):
    """
    Hands each record to the output thread, which writes it with `handlers`, those
    at its level, as they are when it's logged.

    Below `output_level`, the lowest level a log writes at, only `REPLAY_ON_ERROR`
    records are handed over, to be kept: the logger makes every record the
    terminal's replay might keep, and the rest needn't be copied and resolved.
    """

    def __init__(self) -> None:
        super().__init__()
        self.handlers: tuple[logging.Handler, ...] = ()
        self.output_level = logging.NOTSET

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.output_level and not getattr(
            record, "replay_on_error", False
        ):
            return
        # As every handler does: a record that can't be handled, such as one whose
        # arguments don't fit its message, is reported, not raised to the caller.
        try:
            resolved = _resolved(record)
        # ruff: ignore[BLE001]  # Reported by `handleError`, as `QueueHandler` does
        except Exception:
            self.handleError(record)
            return
        write_later(functools.partial(_write, resolved, self.handlers))


def _resolved(record: logging.LogRecord) -> logging.LogRecord:
    """
    A copy of `record` with its message resolved, as its arguments may change
    before the output thread formats it. Its exception stays, for Rich to render
    as a traceback.

    A template message keeps its template, for the terminal handler to style.
    """
    record = copy.copy(record)
    if (template := getattr(record.msg, "template", None)) is not None:
        record.template = template
    record.msg = record.getMessage()
    record.args = None
    return record


def _write(record: logging.LogRecord, handlers: tuple[logging.Handler, ...]) -> None:
    for handler in handlers:
        if record.levelno >= handler.level:
            handler.handle(record)


class _DebugDetail(logging.Filter):
    """
    Let `handler` show tracebacks only at DEBUG, and asyncssh's records only if
    `asyncssh`.
    """

    def __init__(self, handler: logging.Handler) -> None:
        super().__init__()
        self.handler = handler
        self.asyncssh = False

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        if record.name.partition(".")[0] == ASYNCSSH_LOGGER_NAME:
            return self.asyncssh
        if record.exc_info and self.handler.level > logging.DEBUG:
            # A copy, as the record goes on to the other handlers.
            record = copy.copy(record)
            record.exc_info = None
            record.exc_text = None
        return record


class _TerminalHandler(RichHandler):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.detail = _DebugDetail(self)
        self.addFilter(self.detail)

    def render_message(self, record: logging.LogRecord, message: str) -> Text:
        """Style templated parts of message."""
        template: Template | None = getattr(record, "template", None)
        if template is None:
            return Text(message)
        # The format ends with the message.
        return Text(message.removesuffix(record.getMessage())) + rich_text(template)


class _Replay(logging.Handler):
    """
    Hands `target` the records at its level, and keeps the last `capacity`
    `REPLAY_ON_ERROR` records below it, which an ERROR record brings out first.

    Like `MemoryHandler`, but that also hands over what it keeps once it is full,
    and at exit.
    """

    def __init__(self, target: logging.Handler, *, capacity: int) -> None:
        # At DEBUG, so that the logger makes the records to keep.
        super().__init__(logging.DEBUG)
        self.target = target
        self.kept: deque[logging.LogRecord] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.target.level:
            if getattr(record, "replay_on_error", False):
                self.kept.append(record)
            return
        if record.levelno >= logging.ERROR:
            while self.kept:
                self.target.handle(self.kept.popleft())
        self.target.handle(record)


class _FileHandler(LogFile):
    """A run's log file, which shows tracebacks and asyncssh's records as `detail` says."""

    def __init__(self, directory: Path, *, keep: int) -> None:
        super().__init__(directory, keep=keep)
        self.detail = _DebugDetail(self)
        self.addFilter(self.detail)


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: TextIO | None = None,
    debug: bool = False,
    enabled: bool = True,
) -> logging.Logger:
    """Configure EzHPCy's default terminal logger and return it.

    Records are written through `ezhpcy.cli._console.error_console`, or with `stream`,
    a console like it on that stream. Unless `enabled`, the terminal shows none.

    Repeated calls reuse the same handlers, allowing entry points and tests to
    adjust the level without duplicating each emitted record.

    At DEBUG, the terminal shows tracebacks. With `debug` (`--debug`), it also
    shows timestamps and asyncssh's INFO records.

    The records logged before the call are written first, with the previous
    stream and format. The records it kept to show before an error are dropped.
    """
    global _terminal_handler, _terminal_replay, _terminal_enabled
    flush_output()
    _terminal_enabled = enabled

    if _terminal_handler is None or _terminal_replay is None:
        _terminal_handler = _TerminalHandler(
            console=error_console if stream is None else make_console(file=stream),
            show_time=False,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        _terminal_replay = _Replay(_terminal_handler, capacity=REPLAYED_RECORDS)
        _terminal_replay.set_name(TERMINAL_HANDLER_NAME)
    elif stream is not None:
        _terminal_handler.console = make_console(file=stream)
    _terminal_replay.kept.clear()
    _terminal_handler.setLevel(level)
    _terminal_handler.detail.asyncssh = debug
    _terminal_handler.setFormatter(
        logging.Formatter(
            f"%(asctime)s {LOG_FORMAT}" if debug else LOG_FORMAT,
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    return _update_handlers()


def configure_log_file(
    directory: Path | None, *, level: int | str = logging.DEBUG, keep: int = 10
) -> None:
    """
    Also write the log to a new file in `directory`, at `level`, and keep the files
    of the `keep` most recent runs, this one's included. Without a directory, stop
    writing one. The file is made when the first record comes.

    At DEBUG, the file has tracebacks and asyncssh's INFO records.

    The records logged before the call are written first, to the previous file.
    """
    global _file_handler
    handler = None
    if directory is not None:
        handler = _FileHandler(directory, keep=keep)
        handler.set_name(FILE_HANDLER_NAME)
        handler.setLevel(level)
        handler.detail.asyncssh = handler.level <= logging.DEBUG

    flush_output()
    previous, _file_handler = _file_handler, handler
    _update_handlers()
    if previous is not None:
        # Once it has written what it was handed.
        flush_output()
        previous.close()


def label_log_file(label: str) -> None:
    """
    Name this run's log file after `label` too, such as the profile or tunnel alias
    it is for: a name safe in a file name. The last label is the one it keeps.
    """
    if _file_handler is not None:
        _file_handler.relabel(label)


def _update_handlers() -> logging.Logger:
    """
    Hand the records to the configured handlers, from EzHPCy's logger at the
    lowest of their levels, and return that logger.

    asyncssh's INFO records go to them too, if one shows them.
    """
    global _output_handler
    # What writes the records, and the handlers that hand them over: the
    # terminal's go through its replay.
    outputs: list[_TerminalHandler | _FileHandler] = []
    handlers: list[logging.Handler] = []
    if _terminal_enabled and _terminal_handler and _terminal_replay:
        outputs.append(_terminal_handler)
        handlers.append(_terminal_replay)
    if _file_handler is not None:
        outputs.append(_file_handler)
        handlers.append(_file_handler)
    if _output_handler is None:
        _output_handler = _OutputHandler()
        _output_handler.set_name(OUTPUT_HANDLER_NAME)
    _output_handler.handlers = tuple(handlers)
    _output_handler.output_level = min(
        (output.level for output in outputs), default=logging.NOTSET
    )

    package_logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
    # Without a handler, above every level, so that nothing is even handed over.
    package_logger.setLevel(
        min((handler.level for handler in handlers), default=logging.CRITICAL + 1)
    )
    package_logger.propagate = False
    if _output_handler not in package_logger.handlers:
        package_logger.addHandler(_output_handler)

    # Only asyncssh's INFO records (connections, authentication, channels): its
    # debug records are packet-level detail, and it never logs above INFO.
    asyncssh_logger = logging.getLogger(ASYNCSSH_LOGGER_NAME)
    asyncssh_logger.removeHandler(_output_handler)
    if any(output.detail.asyncssh for output in outputs):
        asyncssh_logger.setLevel(logging.INFO)
        asyncssh_logger.addHandler(_output_handler)
    else:
        asyncssh_logger.setLevel(logging.NOTSET)

    return package_logger
