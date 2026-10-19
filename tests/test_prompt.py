"""Tests for `ezhpcy.cli.utils.prompt`."""

import asyncio
import logging
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ezhpcy.cli.utils.prompt import ask
from ezhpcy.console import make_console
from ezhpcy.logging import flush_logs
from tests.support.terminal import SlowLogTerminal, terminal_handler

# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5


def test_ask_returns_the_answer():
    def prompt(text: str, *, suffix: str) -> str:
        return text + suffix

    assert ask(prompt, "answer", suffix="!") == "answer!"


def test_ctrl_c_interrupts_the_prompt_under_asyncio():
    """asyncio's own handler would only cancel the main task, and the prompt,
    blocking the loop, would keep waiting."""
    reached_the_end = []

    def prompt() -> str:
        signal.raise_signal(signal.SIGINT)
        reached_the_end.append(True)
        return "answer"

    async def main() -> str:
        return ask(prompt)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(main())

    assert reached_the_end == []


@pytest.mark.parametrize(
    "interrupt",
    [
        pytest.param(signal.SIGINT, id="SIGINT"),
        # Ctrl+Break, on Windows only.
        pytest.param(
            getattr(signal, "SIGBREAK", None),
            marks=pytest.mark.skipif(
                not hasattr(signal, "SIGBREAK"), reason="Windows only"
            ),
            id="SIGBREAK",
        ),
    ],
)
def test_an_interrupt_at_the_prompt_bypasses_the_commands_handler(
    interrupt: signal.Signals,
):
    """Like `tunnel`'s, which would only act once the prompt returned."""
    handled = []

    def handler(signum: int, _frame: object) -> None:
        handled.append(signum)

    def prompt() -> str:
        signal.raise_signal(interrupt)
        return "answer"

    previous = signal.signal(interrupt, handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            ask(prompt)
        assert signal.getsignal(interrupt) is handler
    finally:
        signal.signal(interrupt, previous)

    assert handled == []


@pytest.mark.parametrize("fails", [False, True])
def test_the_previous_ctrl_c_handler_is_restored(fails: bool):
    previous = signal.getsignal(signal.SIGINT)

    def prompt() -> str:
        if fails:
            raise ValueError("bad prompt")
        return "answer"

    try:
        ask(prompt)
    except ValueError:
        assert fails

    assert signal.getsignal(signal.SIGINT) is previous


def test_the_records_logged_before_a_prompt_come_before_it(
    monkeypatch: pytest.MonkeyPatch,
):
    terminal = SlowLogTerminal()
    monkeypatch.setattr(terminal_handler(), "console", make_console(file=terminal))

    def prompt() -> str:
        terminal.write("Password: ")
        return "answer"

    logging.getLogger("ezhpcy.cli.utils.ssh").warning("Could not save the host key")
    ask(prompt)

    assert flush_logs(WAIT_SECONDS)
    output = terminal.getvalue()
    assert output.index("Could not save the host key") < output.index("Password: ")


_CTRL_C_PROGRAM = textwrap.dedent(
    """
    import asyncio, fcntl, getpass, termios
    from ezhpcy.cli.utils.prompt import ask

    # Make the pseudo-terminal this process's controlling terminal, so getpass
    # opens it and Ctrl+C on it sends SIGINT here.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    async def main():
        ask(getpass.getpass, "Password: ")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("interrupted")
    echo = termios.tcgetattr(0)[3] & termios.ECHO
    print("echo on" if echo else "echo off", flush=True)
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminals")
def test_ctrl_c_at_a_real_password_prompt_ends_cleanly():
    """A real getpass in a terminal, under asyncio: one Ctrl+C ends it, and echo
    is back on."""
    import pty

    controller, terminal = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", _CTRL_C_PROGRAM],
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        start_new_session=True,
        cwd=Path(__file__).parents[1],
    )
    os.close(terminal)
    try:
        assert b"Password: " in _read_until(controller, b"Password: ")
        os.write(controller, b"\x03")
        # Until the process exits and the terminal closes.
        output = _read_until(controller)
        assert process.wait(WAIT_SECONDS) == 0, output
    finally:
        process.kill()
        process.wait()
        os.close(controller)

    assert b"interrupted" in output
    assert b"echo on" in output


def _read_until(fd: int, needle: bytes | None = None) -> bytes:
    """Read from `fd` until `needle` arrives, the other end closes or time runs out."""
    output = b""
    deadline = time.monotonic() + WAIT_SECONDS
    while (needle is None or needle not in output) and (
        remaining := deadline - time.monotonic()
    ) > 0:
        if not select.select([fd], [], [], remaining)[0]:
            break
        try:
            chunk = os.read(fd, 1024)
        # Linux reports a closed pseudo-terminal as an I/O error.
        except OSError:
            break
        if not chunk:
            break
        output += chunk
    return output
