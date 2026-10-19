import io
import logging
import re
import time
from collections.abc import Iterator
from logging.handlers import QueueHandler

import pytest
from rich.console import Console
from rich.logging import RichHandler

from ezhpcy.console import error_console
from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.logging import (
    ASYNCSSH_LOGGER_NAME,
    LOG_QUEUE_HANDLER_NAME,
    configure_logging,
    flush_logs,
)
from tests.support.terminal import GatedTerminal, SlowLogTerminal, terminal_handler

# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5


@pytest.fixture(autouse=True)
def restore_logger() -> Iterator[None]:
    package_logger = logging.getLogger(PACKAGE_NAME)
    previous_level = package_logger.level
    handler = terminal_handler()
    previous_console = handler.console
    previous_formatter = handler.formatter
    yield
    package_logger.setLevel(previous_level)
    handler.console = previous_console
    handler.setFormatter(previous_formatter)


@pytest.fixture
def gated_stream() -> Iterator[GatedTerminal]:
    stream = GatedTerminal()
    configure_logging("INFO", stream=stream)
    yield stream
    # Never leave the log's thread waiting for the next test.
    stream.opened.set()
    assert flush_logs(WAIT_SECONDS)


def test_default_logger_writes_named_records_to_the_terminal() -> None:
    stream = io.StringIO()
    configured = configure_logging("INFO", stream=stream)

    logging.getLogger("ezhpcy.cli.tunnel").info("Worker ready")

    assert configured is logging.getLogger(PACKAGE_NAME)
    assert flush_logs(WAIT_SECONDS)
    rendered = " ".join(stream.getvalue().split())
    assert rendered == "INFO [ezhpcy.cli.tunnel] Worker ready"


def test_default_logger_queues_records_for_a_rich_stderr_handler() -> None:
    package_logger = logging.getLogger(PACKAGE_NAME)
    [queue_handler] = [
        item
        for item in package_logger.handlers
        if item.get_name() == LOG_QUEUE_HANDLER_NAME
    ]
    handler = terminal_handler()

    assert isinstance(queue_handler, QueueHandler)
    assert isinstance(handler, RichHandler)
    assert handler not in package_logger.handlers
    assert isinstance(handler.console, Console)
    assert handler.console.stderr
    assert handler.rich_tracebacks


def test_debug_logger_includes_a_timestamp() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream, include_timestamp=True)

    logging.getLogger("ezhpcy.cli.tunnel").debug("Transport active")

    assert flush_logs(WAIT_SECONDS)
    rendered = " ".join(stream.getvalue().split())
    assert re.fullmatch(
        r"DEBUG \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
        r"\[ezhpcy\.cli\.tunnel\] Transport active",
        rendered,
    )


def test_configure_logging_is_idempotent() -> None:
    package_logger = logging.getLogger(PACKAGE_NAME)
    existing_handlers = list(package_logger.handlers)

    assert configure_logging() is package_logger
    assert configure_logging() is package_logger
    assert package_logger.handlers == existing_handlers


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown level"):
        configure_logging("very-chatty")


def test_debug_logging_shows_asyncssh_info_records() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)

    asyncssh_logger = logging.getLogger(ASYNCSSH_LOGGER_NAME)
    asyncssh_logger.info("Opening SSH connection")
    asyncssh_logger.debug("Sending packet")

    assert flush_logs(WAIT_SECONDS)
    rendered = " ".join(stream.getvalue().split())
    assert rendered == "INFO [asyncssh] Opening SSH connection"


def test_asyncssh_records_are_not_shown_without_debug_logging() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    configure_logging("INFO", stream=stream)

    logging.getLogger(ASYNCSSH_LOGGER_NAME).info("Opening SSH connection")

    assert flush_logs(WAIT_SECONDS)
    assert stream.getvalue() == ""
    assert logging.getLogger(ASYNCSSH_LOGGER_NAME).handlers == []


def test_records_logged_before_reconfiguring_keep_their_stream_and_format() -> None:
    first, second = SlowLogTerminal(), io.StringIO()
    configure_logging("DEBUG", stream=first, include_timestamp=True)
    logging.getLogger("ezhpcy.cli.tunnel").debug("Before")

    configure_logging("INFO", stream=second)
    logging.getLogger("ezhpcy.cli.tunnel").info("After")

    assert flush_logs(WAIT_SECONDS)
    assert re.fullmatch(
        r"DEBUG \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[ezhpcy\.cli\.tunnel\] Before",
        " ".join(first.getvalue().split()),
    )
    assert " ".join(second.getvalue().split()) == "INFO [ezhpcy.cli.tunnel] After"


def test_a_terminal_that_takes_no_output_does_not_block_logging(
    gated_stream: GatedTerminal,
) -> None:
    started = time.perf_counter()
    for number in range(100):
        logging.getLogger("ezhpcy.tunnel.session").info("Record %d", number)

    assert time.perf_counter() - started < 1
    assert not flush_logs(0.05)
    gated_stream.opened.set()
    assert flush_logs(WAIT_SECONDS)
    assert "Record 99" in gated_stream.getvalue()


def test_the_message_is_the_one_at_the_time_of_logging(
    gated_stream: GatedTerminal,
) -> None:
    hosts = ["n-62-11-19"]
    logging.getLogger("ezhpcy.tunnel.session").info("Hosts: %s", hosts)
    hosts.append("changed later")

    gated_stream.opened.set()
    assert flush_logs(WAIT_SECONDS)
    assert "Hosts: ['n-62-11-19']" in gated_stream.getvalue()


def test_console_output_comes_after_the_records_logged_before_it() -> None:
    terminal = SlowLogTerminal()
    configure_logging("INFO", stream=terminal)
    # The CLI's kind of console, writing where the log does.
    console = type(error_console)(file=terminal)

    logging.getLogger("ezhpcy.cli.doctor").warning("Could not read the profile")
    console.print("Error: no tunnel")

    assert flush_logs(WAIT_SECONDS)
    output = terminal.getvalue()
    assert output.index("Could not read the profile") < output.index("Error: no tunnel")


def test_a_flush_from_the_logs_own_thread_returns_at_once() -> None:
    """As a print would, through the CLI's consoles, from within a log record."""
    flushed: list[bool] = []

    class FlushingTerminal(io.StringIO):
        def write(self, text: str) -> int:
            flushed.append(flush_logs(WAIT_SECONDS))
            return super().write(text)

    configure_logging("INFO", stream=FlushingTerminal())
    logging.getLogger("ezhpcy.cli.tunnel").info("Worker ready")

    assert flush_logs(WAIT_SECONDS)
    assert flushed
    assert all(flushed)


def test_a_logged_exception_keeps_its_traceback() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    try:
        raise RuntimeError("worker gone")
    except RuntimeError:
        logging.getLogger("ezhpcy.cli.tunnel").exception("Tunnel stopped")

    assert flush_logs(WAIT_SECONDS)
    rendered = stream.getvalue()
    assert "Traceback" in rendered
    assert "RuntimeError: worker gone" in rendered
