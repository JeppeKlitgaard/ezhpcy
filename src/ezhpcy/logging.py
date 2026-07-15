import logging
from enum import StrEnum
from typing import TextIO

from rich.console import Console
from rich.logging import RichHandler

from ezhpcy.constants import PACKAGE_NAME

LOG_LEVEL_ENVIRONMENT_VARIABLE = "EZHPCY_LOG_LEVEL"
DEFAULT_LOG_LEVEL = logging.INFO
TERMINAL_HANDLER_NAME = "ezhpcy-terminal"
LOG_FORMAT = "[%(name)s] %(message)s"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _resolve_log_level(level: int | str | LogLevel | None) -> int:
    if level is None:
        level = DEFAULT_LOG_LEVEL
    if isinstance(level, int):
        if level < 0:
            raise ValueError("log level must be non-negative")
        return level

    normalized = level.strip().upper()
    resolved = logging.getLevelNamesMapping().get(normalized)
    if resolved is None:
        raise ValueError(
            f"invalid log level {level!r}; set {LOG_LEVEL_ENVIRONMENT_VARIABLE} "
            "to DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )
    return resolved


def configure_logging(
    level: int | str | LogLevel | None = None, *, stream: TextIO | None = None
) -> logging.Logger:
    """Configure ezhpcy's default terminal logger and return it.

    Repeated calls reuse the same handler, allowing entry points and tests to adjust
    the level without duplicating each emitted record.
    """
    logger = logging.getLogger(PACKAGE_NAME)
    logger.setLevel(_resolve_log_level(level))
    logger.propagate = False

    handler = next(
        (
            candidate
            for candidate in logger.handlers
            if candidate.get_name() == TERMINAL_HANDLER_NAME
        ),
        None,
    )
    if handler is not None and not isinstance(handler, RichHandler):
        logger.removeHandler(handler)
        handler.close()
        handler = None

    if handler is None:
        terminal_console = (
            Console(file=stream, highlight=False)
            if stream is not None
            else Console(stderr=True, highlight=False)
        )
        handler = RichHandler(
            console=terminal_console,
            show_time=False,
            show_path=False,
            rich_tracebacks=True,
            markup=False,
        )
        handler.set_name(TERMINAL_HANDLER_NAME)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    elif stream is not None:
        handler.console = Console(file=stream, highlight=False)

    return logger
