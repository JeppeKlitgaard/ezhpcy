"""
EzHPCy's logger.

EzHPCy's modules log through `get_logger`, which also takes a message as a
template (see `ezhpcy.messages`): `logger.info(t"Run {'ssh gpu':suggestion}.")`.
Records carry it as plain text; a handler that styles it finds the template as
`template` on the record's `msg`.

Nothing here configures logging: that is up to the program. `LogFile` is a handler
for a program to add, which writes each run's log to a file of its own.
"""

import logging
lazy import os
lazy import time
lazy from collections.abc import MutableMapping
lazy from contextlib import suppress
lazy from pathlib import Path
lazy from string.templatelib import Template
lazy from typing import Any

lazy from ezhpcy.messages import plain
lazy from ezhpcy.permissions import private_directory

# `extra` for a record that is context for a failure, such as a line of an
# interactive job's output: to show before the next error, if it wasn't shown.
REPLAY_ON_ERROR = {"replay_on_error": True}
LOG_FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s [%(name)s] %(message)s"
LOG_FILE_PATTERN = "ezhpcy-*.log"


class _Logger(logging.LoggerAdapter[logging.Logger]):
    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        return (_LogMessage(msg) if isinstance(msg, Template) else msg), kwargs


def get_logger(name: str) -> _Logger:
    """The logger `name`, which also takes a message as a template."""
    return _Logger(logging.getLogger(name))


class _LogMessage:
    """A template message: plain text to every handler, styled by those that can."""

    def __init__(self, template: Template) -> None:
        self.template = template

    def __str__(self) -> str:
        return plain(self.template)


logger = get_logger(__name__)


class LogFile(logging.FileHandler):
    """
    Writes one run's log file in `directory`, named after when the run started,
    its label (see `relabel`) and its process. The file is made, in a directory
    only the current user can enter, and the oldest runs' removed to keep `keep`,
    when the first record comes: a run that logs nothing leaves no file.
    """

    def __init__(self, directory: Path, *, keep: int = 10) -> None:
        self.directory = directory
        self.started = time.strftime("%Y%m%d-%H%M%S")
        self.keep = keep
        self.failed = False
        super().__init__(self._path(None), encoding="utf-8", delay=True)
        self.setFormatter(
            logging.Formatter(LOG_FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
        )

    def _path(self, label: str | None) -> Path:
        name = "-".join(filter(None, ["ezhpcy", self.started, label, str(os.getpid())]))
        return (self.directory / f"{name}.log").absolute()

    def relabel(self, label: str) -> None:
        """
        Name the file after `label` too, such as the profile or tunnel alias the run
        is for: a name safe in a file name. The file is moved if it's made.
        """
        path = self._path(label)
        # Holds off `emit`, which may be on another thread.
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
