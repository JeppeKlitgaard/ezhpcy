"""
EzHPCy's terminal log, and each run's log file.

Records are handed to the output thread (see `ezhpcy.console`), which formats and
writes them, so that logging never waits for the terminal, and the event loop
with it. `ezhpcy.console.flush_output` waits until they are written.

EzHPCy's modules log through `get_logger`, which also takes a message as a
template (see `ezhpcy.messages`): `logger.info(t"Run {'ssh gpu':suggestion}.")`.
Records carry it as plain text, and the terminal log styles its parts.

Each run can also write its log to a file of its own (`configure_log_file`), at a
level of its own. What a log shows is up to the log, never the code that logs:
tracebacks are shown only at DEBUG, and asyncssh's records by the terminal only
with debug logging (`--debug`), and by the file at DEBUG.

A record logged with `extra=REPLAY_ON_ERROR`, such as a line of an interactive
job's output, is context for a failure: if the terminal doesn't show it, it keeps
the last of them, and shows them before the next ERROR record.
"""

import logging
lazy import copy
lazy import functools
lazy import os
lazy import time
lazy from collections import deque
lazy from collections.abc import MutableMapping
lazy from contextlib import suppress
lazy from pathlib import Path
lazy from string.templatelib import Template
lazy from typing import Any, TextIO

from rich.logging import RichHandler
lazy from rich.text import Text

lazy from ezhpcy.console import (
    error_console,
    flush_output,
    make_console,
    write_later,
)
lazy from ezhpcy.constants import PACKAGE_NAME
lazy from ezhpcy.messages import plain, rich_text
lazy from ezhpcy.permissions import private_directory

TERMINAL_HANDLER_NAME = "ezhpcy-terminal"
FILE_HANDLER_NAME = "ezhpcy-file"
OUTPUT_HANDLER_NAME = "ezhpcy-output"
LOG_FORMAT = "[%(name)s] %(message)s"
LOG_FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s [%(name)s] %(message)s"
LOG_FILE_PATTERN = "ezhpcy-*.log"
ASYNCSSH_LOGGER_NAME = "asyncssh"
# `extra` for a record to show before the next error, if the terminal didn't.
REPLAY_ON_ERROR = {"replay_on_error": True}
# How many of those the terminal keeps: all of an interactive job's output while
# it starts, which echoes the worker script.
REPLAYED_RECORDS = 1000

# Set up by the first `configure_logging` or `configure_log_file`, and kept for
# the process.
_terminal_handler: _TerminalHandler | None = None
_terminal_replay: _Replay | None = None
_terminal_enabled = True
_output_handler: _OutputHandler | None = None
# Replaced by each `configure_log_file`.
_file_handler: _FileHandler | None = None


class _Logger(logging.LoggerAdapter[logging.Logger]):
    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        return (_LogMessage(msg) if isinstance(msg, Template) else msg), kwargs


def get_logger(name: str) -> _Logger:
    """The logger `name`, which also takes a message as a template."""
    return _Logger(logging.getLogger(name))


logger = get_logger(__name__)


class _LogMessage:
    """A template message: plain text to every handler, styled by the terminal's."""

    def __init__(self, template: Template) -> None:
        self.template = template

    def __str__(self) -> str:
        return plain(self.template)


class _OutputHandler(logging.Handler):
    """
    Hands each record to the output thread, which writes it with `handlers`, those
    at its level, as they are when it's logged.
    """

    def __init__(self) -> None:
        super().__init__()
        self.handlers: tuple[logging.Handler, ...] = ()

    def emit(self, record: logging.LogRecord) -> None:
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
    if isinstance(record.msg, _LogMessage):
        record.template = record.msg.template
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


class _FileHandler(logging.FileHandler):
    """
    Writes one run's log file in `directory`, named after when the run started,
    its label (see `label_log_file`) and its process. The file is made, and the
    oldest runs' removed to keep `keep`, when the first record comes: a run that
    logs nothing leaves no file.
    """

    def __init__(self, directory: Path, *, keep: int) -> None:
        self.directory = directory
        self.started = time.strftime("%Y%m%d-%H%M%S")
        self.keep = keep
        self.failed = False
        super().__init__(self._path(None), encoding="utf-8", delay=True)
        self.detail = _DebugDetail(self)
        self.addFilter(self.detail)

    def _path(self, label: str | None) -> Path:
        name = "-".join(filter(None, ["ezhpcy", self.started, label, str(os.getpid())]))
        return (self.directory / f"{name}.log").absolute()

    def relabel(self, label: str) -> None:
        """Name the file after `label`, moving it if it's made."""
        path = self._path(label)
        # Holds off `emit`, in the output thread.
        self.acquire()
        try:
            if self.stream is None:
                self.baseFilename = str(path)
                return
            self.stream.close()
            try:
                Path(self.baseFilename).rename(path)
            except OSError as error:
                logger.debug(t"Could not rename the log file to {path:path}: {error}")
            else:
                self.baseFilename = str(path)
            self.stream = self._open()
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is None:
            if self.failed:
                return
            path = Path(self.baseFilename)
            try:
                private_directory(path.parent)
                _remove_old_logs(path.parent, keep=self.keep - 1)
                self.stream = self._open()
            except OSError as error:
                # Reported once, rather than raised for every record. The warning
                # comes back here, to be dropped with the rest.
                self.failed = True
                logger.warning(t"Could not write the log file {path:path}: {error}")
                return
        super().emit(record)


def _remove_old_logs(directory: Path, *, keep: int) -> None:
    """
    Remove all but the `keep` most recently written log files in `directory`.

    By when they were written, so that a long tunnel's log outlives the runs
    started after it. One that can't be removed, as on Windows while its run still
    writes it, stays.
    """
    logs: list[tuple[float, Path]] = []
    for path in directory.glob(LOG_FILE_PATTERN):
        with suppress(OSError):
            logs.append((path.stat().st_mtime, path))
    logs.sort(reverse=True)
    for _, path in logs[keep:]:
        with suppress(OSError):
            path.unlink()


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: TextIO | None = None,
    debug: bool = False,
    enabled: bool = True,
) -> logging.Logger:
    """Configure EzHPCy's default terminal logger and return it.

    Records are written through `ezhpcy.console.error_console`, or with `stream`,
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
    if keep < 1:
        raise ValueError(f"keep must be at least 1, not {keep}")
    handler = None
    if directory is not None:
        handler = _FileHandler(directory, keep=keep)
        handler.set_name(FILE_HANDLER_NAME)
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(LOG_FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
        )
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

    package_logger = logging.getLogger(PACKAGE_NAME)
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
