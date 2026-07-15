import io
import logging

import pytest
from rich.console import Console
from rich.logging import RichHandler

from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.logging import (
    TERMINAL_HANDLER_NAME,
    configure_logging,
)


@pytest.fixture(autouse=True)
def restore_logger() -> None:
    package_logger = logging.getLogger(PACKAGE_NAME)
    previous_level = package_logger.level
    handler = next(
        item
        for item in package_logger.handlers
        if item.get_name() == TERMINAL_HANDLER_NAME
    )
    assert isinstance(handler, RichHandler)
    previous_console = handler.console
    yield
    package_logger.setLevel(previous_level)
    handler.console = previous_console


def test_default_logger_writes_named_records_to_the_terminal() -> None:
    stream = io.StringIO()
    configured = configure_logging("INFO", stream=stream)

    logging.getLogger("ezhpcy.cli.tunnel.compute").info("Worker ready")

    assert configured is logging.getLogger(PACKAGE_NAME)
    rendered = " ".join(stream.getvalue().split())
    assert rendered == "INFO [ezhpcy.cli.tunnel.compute] Worker ready"


def test_default_logger_uses_a_rich_stderr_handler() -> None:
    package_logger = logging.getLogger(PACKAGE_NAME)
    handler = next(
        item
        for item in package_logger.handlers
        if item.get_name() == TERMINAL_HANDLER_NAME
    )

    assert isinstance(handler, RichHandler)
    assert isinstance(handler.console, Console)
    assert handler.console.stderr
    assert handler.rich_tracebacks


def test_configure_logging_is_idempotent() -> None:
    package_logger = logging.getLogger(PACKAGE_NAME)
    existing_handlers = list(package_logger.handlers)

    assert configure_logging() is package_logger
    assert configure_logging() is package_logger
    assert package_logger.handlers == existing_handlers


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid log level"):
        configure_logging("very-chatty")
