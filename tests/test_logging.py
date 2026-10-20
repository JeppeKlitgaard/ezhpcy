import io
import logging
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ezhpcy.console import flush_output, make_console
from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.logging import (
    ASYNCSSH_LOGGER_NAME,
    OUTPUT_HANDLER_NAME,
    REPLAY_ON_ERROR,
    REPLAYED_RECORDS,
    configure_log_file,
    configure_logging,
    get_logger,
    label_log_file,
)
from tests.support.terminal import (
    GatedTerminal,
    SlowLogTerminal,
    log_file_path,
    log_handlers,
    terminal_handler,
)
from tests.support.tunnel import LogRecords

# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5


@pytest.fixture(autouse=True)
def restore_logger() -> Iterator[None]:
    package_logger = logging.getLogger(PACKAGE_NAME)
    previous_level = package_logger.level
    handler = terminal_handler()
    previous_handler_level = handler.level
    previous_console = handler.console
    previous_formatter = handler.formatter
    yield
    package_logger.setLevel(previous_level)
    configure_logging(previous_handler_level)
    handler.console = previous_console
    handler.setFormatter(previous_formatter)


@pytest.fixture
def gated_stream() -> Iterator[GatedTerminal]:
    stream = GatedTerminal()
    configure_logging("INFO", stream=stream)
    yield stream
    # Never leave the output thread waiting for the next test.
    stream.opened.set()
    assert flush_output(WAIT_SECONDS)


def test_default_logger_writes_named_records_to_the_terminal() -> None:
    stream = io.StringIO()
    configured = configure_logging("INFO", stream=stream)

    logging.getLogger("ezhpcy.cli.tunnel").info("Worker ready")

    assert configured is logging.getLogger(PACKAGE_NAME)
    assert flush_output(WAIT_SECONDS)
    rendered = " ".join(stream.getvalue().split())
    assert rendered == "INFO [ezhpcy.cli.tunnel] Worker ready"


def colour_terminal() -> io.StringIO:
    """Log to a colour terminal, whatever `NO_COLOR` says."""
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    terminal_handler().console = make_console(
        file=stream,
        force_terminal=True,
        color_system="standard",
        no_color=False,
        # So that no record wraps.
        width=200,
    )
    return stream


def test_a_log_message_is_styled_on_the_terminal() -> None:
    stream = colour_terminal()

    get_logger("ezhpcy.tunnel.session").info(
        t"Connect with {'ssh scratch':suggestion}."
    )

    assert flush_output(WAIT_SECONDS)
    output = stream.getvalue()
    assert "\x1b[1;32m'ssh scratch'\x1b[0m" in output
    rendered = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())
    assert rendered == "INFO [ezhpcy.tunnel.session] Connect with 'ssh scratch'."


def test_only_the_marked_parts_of_a_log_message_are_styled() -> None:
    """Rich's own highlighting would style the digits of a host name."""
    stream = colour_terminal()
    logger = get_logger("ezhpcy.tunnel.session")

    logger.info("Waiting for worker SSH endpoint %s.", "n-62-12-19:58395")
    logger.info(t"Waiting for worker SSH endpoint {'n-62-12-19:58395':endpoint}.")

    assert flush_output(WAIT_SECONDS)
    unmarked, marked = stream.getvalue().splitlines()
    assert "] Waiting for worker SSH endpoint n-62-12-19:58395. " in unmarked
    assert (
        "endpoint \x1b[1;36mn-62-12-19\x1b[0m\x1b[1;37m:\x1b[0m\x1b[36m58395\x1b[0m. "
        in marked
    )


def test_a_template_message_is_plain_text_to_other_handlers() -> None:
    records = LogRecords()
    logger = logging.getLogger("ezhpcy.tunnel.session")
    logger.addHandler(records)
    try:
        get_logger("ezhpcy.tunnel.session").warning(
            t"{'ssh gpu':suggestion} will not reach {'tunnel':value}."
        )
    finally:
        logger.removeHandler(records)

    [record] = records.records
    assert record.getMessage() == "'ssh gpu' will not reach \"tunnel\"."
    # The call site, not the adapter.
    assert record.funcName == "test_a_template_message_is_plain_text_to_other_handlers"


def test_configure_logging_is_idempotent() -> None:
    package_logger = logging.getLogger(PACKAGE_NAME)
    existing_handlers = list(package_logger.handlers)

    assert configure_logging() is package_logger
    assert configure_logging() is package_logger
    assert package_logger.handlers == existing_handlers


def test_debug_logging_shows_asyncssh_info_records() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream, debug=True)

    asyncssh_logger = logging.getLogger(ASYNCSSH_LOGGER_NAME)
    asyncssh_logger.info("Opening SSH connection")
    asyncssh_logger.debug("Sending packet")

    assert flush_output(WAIT_SECONDS)
    rendered = " ".join(stream.getvalue().split())
    assert re.fullmatch(r"INFO \S+ \S+ \[asyncssh\] Opening SSH connection", rendered)


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
def test_asyncssh_records_are_not_shown_without_debug_logging(level: str) -> None:
    """Not even at DEBUG: only `--debug` asks for them."""
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream, debug=True)
    configure_logging(level, stream=stream)

    logging.getLogger(ASYNCSSH_LOGGER_NAME).info("Opening SSH connection")

    assert flush_output(WAIT_SECONDS)
    assert stream.getvalue() == ""
    assert logging.getLogger(ASYNCSSH_LOGGER_NAME).handlers == []


def test_records_logged_before_reconfiguring_keep_their_stream_and_format() -> None:
    first, second = SlowLogTerminal(), io.StringIO()
    configure_logging("DEBUG", stream=first, debug=True)
    logging.getLogger("ezhpcy.cli.tunnel").debug("Before")

    configure_logging("INFO", stream=second)
    logging.getLogger("ezhpcy.cli.tunnel").info("After")

    assert flush_output(WAIT_SECONDS)
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
    assert not flush_output(0.05)
    gated_stream.opened.set()
    assert flush_output(WAIT_SECONDS)
    assert "Record 99" in gated_stream.getvalue()


def test_a_record_whose_arguments_do_not_fit_is_reported_not_raised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    As by the standard library's handlers: logging never fails its caller. Handled
    directly, as pytest's own handler raises instead.
    """
    [output_handler] = [
        handler
        for handler in logging.getLogger(PACKAGE_NAME).handlers
        if handler.get_name() == OUTPUT_HANDLER_NAME
    ]
    record = logging.LogRecord(
        "ezhpcy.tunnel.session",
        logging.INFO,
        __file__,
        1,
        "count %d",
        ("not a number",),
        None,
    )

    output_handler.handle(record)

    assert "--- Logging error ---" in capsys.readouterr().err


def test_the_message_is_the_one_at_the_time_of_logging(
    gated_stream: GatedTerminal,
) -> None:
    hosts = ["n-62-11-19"]
    logging.getLogger("ezhpcy.tunnel.session").info("Hosts: %s", hosts)
    hosts.append("changed later")

    gated_stream.opened.set()
    assert flush_output(WAIT_SECONDS)
    assert "Hosts: ['n-62-11-19']" in gated_stream.getvalue()


def test_console_output_comes_after_the_records_logged_before_it() -> None:
    terminal = SlowLogTerminal()
    configure_logging("INFO", stream=terminal)
    # The CLI's kind of console, writing where the log does.
    console = make_console(file=terminal)

    logging.getLogger("ezhpcy.cli.doctor").warning("Could not read the profile")
    console.print("Error: no tunnel")

    assert flush_output(WAIT_SECONDS)
    output = terminal.getvalue()
    assert output.index("Could not read the profile") < output.index("Error: no tunnel")


def test_a_flush_from_the_output_thread_returns_at_once() -> None:
    """As a print would, through the CLI's consoles, from within a log record."""
    flushed: list[bool] = []

    class FlushingTerminal(io.StringIO):
        def write(self, text: str) -> int:
            flushed.append(flush_output(WAIT_SECONDS))
            return super().write(text)

    configure_logging("INFO", stream=FlushingTerminal())
    logging.getLogger("ezhpcy.cli.tunnel").info("Worker ready")

    assert flush_output(WAIT_SECONDS)
    assert flushed
    assert all(flushed)


def log_exception() -> None:
    try:
        raise RuntimeError("worker gone")
    except RuntimeError:
        logging.getLogger("ezhpcy.cli.tunnel").exception("Tunnel stopped")


def test_a_logged_exception_keeps_its_traceback_at_debug() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)

    log_exception()

    assert flush_output(WAIT_SECONDS)
    rendered = stream.getvalue()
    assert "Traceback" in rendered
    assert "RuntimeError: worker gone" in rendered


# The log file


@pytest.fixture
def terminal() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    return stream


def read_log(path: Path) -> str:
    assert flush_output(WAIT_SECONDS)
    return path.read_text(encoding="utf-8")


def test_a_disabled_terminal_shows_no_records(
    terminal: io.StringIO, tmp_path: Path
) -> None:
    configure_logging("INFO", enabled=False)
    configure_log_file(tmp_path)
    path = log_file_path()

    logging.getLogger("ezhpcy.cli.tunnel").warning("Worker gone")

    assert read_log(path).endswith("] Worker gone\n")
    assert terminal.getvalue() == ""


def test_with_no_log_nothing_is_logged() -> None:
    configure_logging("DEBUG", enabled=False)

    assert not logging.getLogger("ezhpcy.cli.tunnel").isEnabledFor(logging.CRITICAL)
    assert logging.getLogger(ASYNCSSH_LOGGER_NAME).handlers == []


def test_the_log_file_has_debug_records_the_terminal_does_not(
    terminal: io.StringIO, tmp_path: Path
) -> None:
    configure_log_file(tmp_path / "log")
    path = log_file_path()
    logger = logging.getLogger("ezhpcy.cli.tunnel")

    logger.debug("Transport active")
    logger.info("Worker ready")

    log = read_log(path)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} DEBUG    "
        r"\[ezhpcy\.cli\.tunnel\] Transport active\n"
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} INFO     "
        r"\[ezhpcy\.cli\.tunnel\] Worker ready\n",
        log,
    )
    assert (
        " ".join(terminal.getvalue().split()) == "INFO [ezhpcy.cli.tunnel] Worker ready"
    )


def test_only_logs_at_debug_show_tracebacks(
    terminal: io.StringIO, tmp_path: Path
) -> None:
    configure_log_file(tmp_path)
    path = log_file_path()

    log_exception()

    log = read_log(path)
    assert "RuntimeError: worker gone" in log
    rendered = " ".join(terminal.getvalue().split())
    assert rendered == "ERROR [ezhpcy.cli.tunnel] Tunnel stopped"


def test_a_log_file_at_debug_has_asyncssh_records(
    terminal: io.StringIO, tmp_path: Path
) -> None:
    configure_log_file(tmp_path)
    path = log_file_path()

    logging.getLogger(ASYNCSSH_LOGGER_NAME).info("Opening SSH connection")

    assert read_log(path).endswith("INFO     [asyncssh] Opening SSH connection\n")
    assert terminal.getvalue() == ""


def test_a_log_file_above_debug_has_no_asyncssh_records(tmp_path: Path) -> None:
    configure_log_file(tmp_path, level="INFO")
    path = log_file_path()

    logging.getLogger(ASYNCSSH_LOGGER_NAME).info("Opening SSH connection")
    logging.getLogger("ezhpcy.cli.tunnel").info("Worker ready")

    assert "asyncssh" not in read_log(path)
    assert logging.getLogger(ASYNCSSH_LOGGER_NAME).handlers == []


# What led up to an error


def lines(terminal: io.StringIO) -> list[str]:
    assert flush_output(WAIT_SECONDS)
    return [" ".join(line.split()) for line in terminal.getvalue().splitlines()]


def test_an_error_brings_out_the_replayed_records_the_terminal_did_not_show(
    terminal: io.StringIO,
) -> None:
    logger = logging.getLogger("ezhpcy.tunnel.session")

    logger.debug("Job output", extra=REPLAY_ON_ERROR)
    logger.debug("Polling the scheduler")
    logger.info("Waiting for worker")
    logger.warning("Worker is slow")
    assert lines(terminal) == [
        "INFO [ezhpcy.tunnel.session] Waiting for worker",
        "WARNING [ezhpcy.tunnel.session] Worker is slow",
    ]
    logger.error("Worker gone")
    logger.error("Tunnel stopped")

    # Only the one to replay, and only once.
    assert lines(terminal)[2:] == [
        "DEBUG [ezhpcy.tunnel.session] Job output",
        "ERROR [ezhpcy.tunnel.session] Worker gone",
        "ERROR [ezhpcy.tunnel.session] Tunnel stopped",
    ]


def test_the_terminal_keeps_only_the_last_records_to_replay(
    terminal: io.StringIO,
) -> None:
    logger = logging.getLogger("ezhpcy.tunnel.session")

    for number in range(REPLAYED_RECORDS + 1):
        logger.debug("Line %d", number, extra=REPLAY_ON_ERROR)
    logger.error("Worker gone")

    shown = lines(terminal)
    assert len(shown) == REPLAYED_RECORDS + 1
    assert shown[0] == "DEBUG [ezhpcy.tunnel.session] Line 1"


def test_a_terminal_at_debug_shows_records_once() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    logger = logging.getLogger("ezhpcy.tunnel.session")

    logger.debug("Job output", extra=REPLAY_ON_ERROR)
    logger.error("Worker gone")

    assert lines(stream) == [
        "DEBUG [ezhpcy.tunnel.session] Job output",
        "ERROR [ezhpcy.tunnel.session] Worker gone",
    ]


def test_records_kept_before_reconfiguring_are_not_brought_out(
    terminal: io.StringIO,
) -> None:
    logger = logging.getLogger("ezhpcy.tunnel.session")
    logger.debug("From before", extra=REPLAY_ON_ERROR)

    configure_logging("INFO", stream=terminal)
    logger.error("Worker gone")

    assert lines(terminal) == ["ERROR [ezhpcy.tunnel.session] Worker gone"]


def test_records_kept_are_not_brought_out_at_exit(terminal: io.StringIO) -> None:
    logging.getLogger("ezhpcy.tunnel.session").debug(
        "Job output", extra=REPLAY_ON_ERROR
    )

    # As at exit.
    for handler in log_handlers().values():
        handler.flush()

    assert lines(terminal) == []


def test_a_run_that_logs_nothing_leaves_no_log_file(tmp_path: Path) -> None:
    configure_log_file(tmp_path / "log")
    path = log_file_path()

    configure_log_file(None)

    assert not path.parent.exists()


def test_once_stopped_the_log_file_takes_no_more_records(tmp_path: Path) -> None:
    configure_log_file(tmp_path)
    path = log_file_path()
    logger = logging.getLogger("ezhpcy.cli.tunnel")
    logger.info("Before")

    configure_log_file(None)
    logger.info("After")

    log = read_log(path)
    assert "Before" in log
    assert "After" not in log


def test_the_most_recently_written_log_files_are_kept(tmp_path: Path) -> None:
    now = time.time()
    # Named by when they started, but the oldest was written last, as a long
    # tunnel's log is.
    ages = {
        "ezhpcy-20260101-000000-1.log": 1,
        "ezhpcy-20260102-000000-2.log": 3,
        "ezhpcy-20260103-000000-3.log": 2,
        "other.log": 100,
    }
    for name, age in ages.items():
        (tmp_path / name).touch()
        os.utime(tmp_path / name, (now - age, now - age))

    configure_log_file(tmp_path, keep=3)
    path = log_file_path()
    logging.getLogger("ezhpcy.cli.tunnel").info("Worker ready")

    assert flush_output(WAIT_SECONDS)
    assert sorted(item.name for item in tmp_path.iterdir()) == sorted(
        [
            "ezhpcy-20260101-000000-1.log",
            "ezhpcy-20260103-000000-3.log",
            path.name,
            "other.log",
        ]
    )


def test_the_log_file_is_named_after_its_label(tmp_path: Path) -> None:
    configure_log_file(tmp_path)

    label_log_file("base")
    logging.getLogger("ezhpcy.cli.tunnel").info("Worker ready")

    assert flush_output(WAIT_SECONDS)
    [path] = tmp_path.iterdir()
    assert re.fullmatch(rf"ezhpcy-\d{{8}}-\d{{6}}-base-{os.getpid()}\.log", path.name)


def test_a_log_file_already_made_moves_to_its_new_label(tmp_path: Path) -> None:
    configure_log_file(tmp_path)
    logger = logging.getLogger("ezhpcy.cli.tunnel")
    logger.info("Before")
    assert flush_output(WAIT_SECONDS)

    label_log_file("base")
    label_log_file("scratch")
    logger.info("After")

    assert flush_output(WAIT_SECONDS)
    [path] = tmp_path.iterdir()
    assert path == log_file_path()
    assert "-scratch-" in path.name
    log = path.read_text(encoding="utf-8")
    assert log.index("Before") < log.index("After")


def test_a_log_file_that_cannot_be_written_is_reported_once(
    terminal: io.StringIO, tmp_path: Path
) -> None:
    (tmp_path / "file").touch()
    configure_log_file(tmp_path / "file" / "log")
    logger = logging.getLogger("ezhpcy.cli.tunnel")

    logger.info("Worker ready")
    logger.info("Tunnel ready")

    assert flush_output(WAIT_SECONDS)
    rendered = terminal.getvalue()
    assert rendered.count("Could not write the log file") == 1
    assert "Worker ready" in rendered
    assert "Tunnel ready" in rendered
