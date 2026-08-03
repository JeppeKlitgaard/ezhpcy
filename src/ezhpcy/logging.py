import logging
from typing import TextIO

from rich.console import Console
from rich.logging import RichHandler

from ezhpcy.constants import PACKAGE_NAME

TERMINAL_HANDLER_NAME = "ezhpcy-terminal"
LOG_FORMAT = "[%(name)s] %(message)s"


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: TextIO | None = None,
    include_timestamp: bool = False,
) -> logging.Logger:
    """Configure EzHPCy's default terminal logger and return it.

    Repeated calls reuse the same handler, allowing entry points and tests to adjust
    the level without duplicating each emitted record.
    """
    logger = logging.getLogger(PACKAGE_NAME)
    logger.setLevel(level)
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
        logger.addHandler(handler)
    elif stream is not None:
        handler.console = Console(file=stream, highlight=False)

    handler.setFormatter(
        logging.Formatter(
            f"%(asctime)s {LOG_FORMAT}" if include_timestamp else LOG_FORMAT,
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    return logger
