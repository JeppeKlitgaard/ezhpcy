import logging
lazy from typing import TextIO

lazy from rich.console import Console
lazy from rich.logging import RichHandler

lazy from ezhpcy.constants import PACKAGE_NAME

TERMINAL_HANDLER_NAME = "ezhpcy-terminal"
LOG_FORMAT = "[%(name)s] %(message)s"
ASYNCSSH_LOGGER_NAME = "asyncssh"


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: TextIO | None = None,
    include_timestamp: bool = False,
) -> logging.Logger:
    """Configure EzHPCy's default terminal logger and return it.

    Repeated calls reuse the same handler, allowing entry points and tests to adjust
    the level without duplicating each emitted record.

    At DEBUG, asyncssh's INFO records go to the same handler.
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

    # Redirect asyncssh logs.
    # asyncssh never logs above INFO, so it is only shown with debug logging, and
    # only at INFO (connections, authentication, channels): its own debug
    # records are packet-level detail.
    asyncssh_logger = logging.getLogger(ASYNCSSH_LOGGER_NAME)
    for candidate in list(asyncssh_logger.handlers):
        if candidate.get_name() == TERMINAL_HANDLER_NAME:
            asyncssh_logger.removeHandler(candidate)
    if logger.isEnabledFor(logging.DEBUG):
        asyncssh_logger.setLevel(logging.INFO)
        asyncssh_logger.addHandler(handler)
    else:
        asyncssh_logger.setLevel(logging.NOTSET)

    return logger
