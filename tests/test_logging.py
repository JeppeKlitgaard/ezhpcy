import io
import logging
import re
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.logging import RichHandler
from typer.testing import CliRunner

from ezhpcy.cli import app
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
    previous_formatter = handler.formatter
    yield
    package_logger.setLevel(previous_level)
    handler.console = previous_console
    handler.setFormatter(previous_formatter)


def test_default_logger_writes_named_records_to_the_terminal() -> None:
    stream = io.StringIO()
    configured = configure_logging("INFO", stream=stream)

    logging.getLogger("ezhpcy.cli.compute").info("Worker ready")

    assert configured is logging.getLogger(PACKAGE_NAME)
    rendered = " ".join(stream.getvalue().split())
    assert rendered == "INFO [ezhpcy.cli.compute] Worker ready"


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


def test_debug_logger_includes_a_timestamp() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream, include_timestamp=True)

    logging.getLogger("ezhpcy.cli.compute").debug("Transport active")

    rendered = " ".join(stream.getvalue().split())
    assert re.fullmatch(
        r"DEBUG \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
        r"\[ezhpcy\.cli\.compute\] Transport active",
        rendered,
    )


def test_root_debug_option_overrides_logging_configuration() -> None:
    with patch("ezhpcy.cli.configure_logging") as configure:
        result = CliRunner().invoke(app, ["--debug", "version"])

    assert result.exit_code == 0, result.output
    configure.assert_called_once_with(logging.DEBUG, include_timestamp=True)


def test_debug_env_var_overrides_logging_configuration_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EZHPCY_DEBUG", "1")

    with patch("ezhpcy.cli.configure_logging") as configure:
        result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    configure.assert_called_once_with(logging.DEBUG, include_timestamp=True)


def test_root_help_exposes_debug_option() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "--debug" in result.output


def test_root_debug_help_exposes_its_env_var() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "EZHPCY_DEBUG" in result.output


def test_configure_logging_is_idempotent() -> None:
    package_logger = logging.getLogger(PACKAGE_NAME)
    existing_handlers = list(package_logger.handlers)

    assert configure_logging() is package_logger
    assert configure_logging() is package_logger
    assert package_logger.handlers == existing_handlers


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown level"):
        configure_logging("very-chatty")
