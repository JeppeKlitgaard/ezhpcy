"""The live display in `ezhpcy.console`, under the log, and around prompts."""

import io
import sys
import threading
import time

import pytest
from rich.text import Text

from ezhpcy import console as consoles
from ezhpcy.cli.utils.prompt import ask
from ezhpcy.console import (
    flush_output,
    hide_live_display,
    live_display,
    make_console,
)
from ezhpcy.logging import get_logger
from tests.support.terminal import GatedTerminal, use_terminal

# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5
# Longer than two redraws, at four a second.
REDRAWS_SECONDS = 0.6
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"


def status() -> Text:
    return Text("Tunnel status")


def test_records_logged_under_a_live_display_are_written_above_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Rich draws the display holding its lock, which each record takes to print
    above it: had drawing it waited for the log, the two would wait forever.
    """
    terminal = io.StringIO()
    use_terminal(monkeypatch, terminal)
    logger = get_logger("ezhpcy.tunnel.session")
    results: list[bool] = []

    def log_under_display() -> None:
        with live_display(status) as shown:
            results.append(shown)
            numbers = iter(range(1_000_000))
            deadline = time.monotonic() + REDRAWS_SECONDS
            while time.monotonic() < deadline:
                logger.info(f"Record {next(numbers)}")
                time.sleep(0.001)
            results.append(flush_output(WAIT_SECONDS))

    # In a thread, so that a test that fails doesn't hang.
    thread = threading.Thread(target=log_under_display, daemon=True)
    thread.start()
    thread.join(2 * WAIT_SECONDS)

    assert not thread.is_alive()
    assert results == [True, True]
    assert flush_output(WAIT_SECONDS)
    output = terminal.getvalue()
    assert "Record 0" in output
    assert output.count("Tunnel status") > 1
    # Hidden at the end, the terminal has its cursor back.
    assert output.rindex(SHOW_CURSOR) > output.rindex(HIDE_CURSOR)


def test_a_console_that_is_not_a_terminal_shows_no_live_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(consoles, "error_console", make_console(file=stream))

    with live_display(status) as shown:
        assert not shown

    assert stream.getvalue() == ""


def test_a_prompt_hides_the_live_display_while_it_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_terminal(monkeypatch, io.StringIO())
    draws = 0

    def count_draws() -> Text:
        nonlocal draws
        draws += 1
        return status()

    def prompt() -> int:
        before = draws
        time.sleep(REDRAWS_SECONDS)
        return draws - before

    with live_display(count_draws):
        drawn_while_asked = ask(prompt)
        before = draws
        time.sleep(REDRAWS_SECONDS)
        drawn_after = draws - before

    assert drawn_while_asked == 0
    # And it is back after.
    assert drawn_after > 0


def test_showing_and_hiding_the_live_display_never_wait_for_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As the tunnel does on the event loop, while the terminal takes no output."""
    terminal = GatedTerminal()
    use_terminal(monkeypatch, terminal)
    started = time.monotonic()

    with live_display(status) as shown:
        time.sleep(REDRAWS_SECONDS)

    waited = time.monotonic() - started
    terminal.opened.set()
    assert shown
    assert waited < REDRAWS_SECONDS + 0.5
    assert flush_output(WAIT_SECONDS)
    assert "Tunnel status" in terminal.getvalue()


def test_hiding_the_live_display_to_quit_gives_the_terminal_its_cursor_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = io.StringIO()
    use_terminal(monkeypatch, terminal)

    with live_display(status):
        hide_live_display()
        assert flush_output(WAIT_SECONDS)
        output = terminal.getvalue()

    assert output.rindex(SHOW_CURSOR) > output.rindex(HIDE_CURSOR)


def test_a_display_that_fails_to_draw_is_hidden_its_error_printed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = io.StringIO()
    use_terminal(monkeypatch, terminal)
    errors = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", errors)
    draws = 0

    def failing_once_drawn() -> Text:
        nonlocal draws
        draws += 1
        # Made, shown and redrawn once, then failing.
        if draws > 3:
            raise RuntimeError("renderer bug")
        return status()

    with live_display(failing_once_drawn):
        time.sleep(2 * REDRAWS_SECONDS)
        assert flush_output(WAIT_SECONDS)
        # A prompt after it failed doesn't bring it back.
        ask(lambda: None)
        time.sleep(REDRAWS_SECONDS)

    assert flush_output(WAIT_SECONDS)
    assert errors.getvalue().count("RuntimeError: renderer bug") == 1
    # Hidden, the terminal has its cursor back.
    output = terminal.getvalue()
    assert output.rindex(SHOW_CURSOR) > output.rindex(HIDE_CURSOR)
