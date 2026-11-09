"""
A tunnel's actions as keys in its status panel: reading keys beside the panel,
asking for inputs in the panel, what it shows, and pressing a key in a real run.
"""

import asyncio
import io
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import blessed
import pytest
from blessed.keyboard import Keystroke
from pydantic import TypeAdapter
from rich.console import Console

import ezhpcy
from ezhpcy import api
from ezhpcy.cli import _console as consoles
from ezhpcy.cli._tunnel_actions import ActionMenu, describe
from ezhpcy.cli._tunnel_status import status_panel
from ezhpcy.cli.utils.keys import (
    BACKSPACE,
    ENTER,
    ESCAPE,
    key_of,
    read_keys,
    restore_terminal,
)
from tests.support.terminal import use_terminal
from tests.test_tunnel_status import LOGIN, NOW, SERVING, TITLE

RUN_ID = "abc"
WAIT_SECONDS = 5


def amount_input(**fields: Any) -> api.IntegerParameter:
    return api.IntegerParameter(type="integer", min=1, max=10, **fields)


def action(**fields: Any) -> api.Action:
    return api.Action.model_validate({"run": {"command": ["x"]}, **fields})


@dataclass
class FakeRunningApp:
    """As much of a `RunningApp` as the menu uses."""

    actions: dict[str, api.Action]
    state: api.AppState = api.AppState.READY
    runs: list[api.ActionRun] = field(default_factory=list)
    ran: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    result: api.ActionRun | Exception | None = None
    app: str = "lab v1.0"

    @property
    def process(self) -> Any:
        return type("Process", (), {"run_id": RUN_ID})

    @property
    def status(self) -> api.AppStatus:
        return api.AppStatus(
            name="lab",
            version="v1.0",
            state=self.state,
            run_id=RUN_ID,
            actions=tuple(self.runs),
        )

    async def run_action(self, name: str, /, **inputs: object) -> api.ActionRun:
        self.ran.append((name, inputs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result or api.ActionRun(
            action=name,
            state=api.ActionState.SUCCEEDED,
            started=NOW,
            finished=NOW + 2,
            exit_status=0,
        )


def menu_of(**actions: api.Action) -> tuple[ActionMenu, FakeRunningApp]:
    menu = ActionMenu()
    running = FakeRunningApp(actions)
    menu.add(running)  # pyrefly: ignore[bad-argument-type]
    return menu, running


async def press(menu: ActionMenu, *pressed: str) -> None:
    for key in pressed:
        for character in [key] if key in (ENTER, BACKSPACE, ESCAPE) else key:
            menu.on_key(character)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def shown(menu: ActionMenu, status: api.AppStatus | None = None) -> list[str]:
    stream = io.StringIO()
    job = replace(SERVING, attachments=(status or menu.entries[0].running.status,))
    panel = status_panel(job, title=TITLE, login=LOGIN, now=NOW, menu=menu)
    Console(file=stream, width=120, color_system=None).print(panel)
    return [
        " ".join(line.strip("│|").split()) for line in stream.getvalue().splitlines()
    ]


@pytest.mark.parametrize(
    ("keystroke", "key"),
    [
        (Keystroke("a"), "a"),
        (Keystroke("å"), "å"),
        (Keystroke("→"), "→"),
        (Keystroke(" "), " "),
        (Keystroke("\r", name="KEY_ENTER"), ENTER),
        (Keystroke("\x7f", name="KEY_BACKSPACE"), BACKSPACE),
        (Keystroke("\x1b", name="KEY_ESCAPE"), ESCAPE),
        # None of these types anything, or cancels.
        (Keystroke("\x1b[A", name="KEY_UP"), None),
        (Keystroke("\x1b[3~", name="KEY_DELETE"), None),
        (Keystroke("\x01", name="KEY_CTRL_A"), None),
        (Keystroke("\x01"), None),
    ],
)
def test_what_a_keystroke_is(keystroke: Keystroke, key: str | None) -> None:
    assert key_of(keystroke) == key


class FakeTerminal:
    """As much of a `blessed.Terminal` as reading keys uses."""

    typed: ClassVar[list[Keystroke]] = []
    in_cbreak: ClassVar[list[str]] = []

    # While set, making one waits for it, as for a terminal slow to answer.
    answering: ClassVar[threading.Event | None] = None

    def __init__(self, *, stream: object) -> None:
        assert stream is sys.__stderr__
        if self.answering is not None:
            self.answering.wait(WAIT_SECONDS)

    @contextmanager
    def cbreak(self) -> Generator[None]:
        self.in_cbreak.append("entered")
        try:
            yield
        finally:
            self.in_cbreak.append("left")

    async def async_inkey(self) -> Keystroke:
        # ruff: ignore[ASYNC110]  # Tests add to it, as a terminal would
        while not self.typed:
            await asyncio.sleep(0.005)
        return self.typed.pop(0)


class TerminalStdin:
    """A stdin that is a terminal, in the foreground, with no real keyboard."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 0


@pytest.fixture
def fake_terminal(monkeypatch: pytest.MonkeyPatch) -> type[FakeTerminal]:
    # Never the real one: blessed reads `sys.__stdin__`, which pytest leaves alone,
    # so that under an interactive run it would read the keyboard.
    monkeypatch.setattr(blessed, "Terminal", FakeTerminal)
    monkeypatch.setattr(sys, "stdin", TerminalStdin())
    monkeypatch.setattr(os, "tcgetpgrp", lambda _fd: os.getpgrp(), raising=False)
    monkeypatch.setattr(FakeTerminal, "typed", [])
    monkeypatch.setattr(FakeTerminal, "in_cbreak", [])
    monkeypatch.setattr(FakeTerminal, "answering", None)
    return FakeTerminal


async def in_cbreak_mode(fake_terminal: type[FakeTerminal]) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        # ruff: ignore[ASYNC110]  # It changes on the thread that reads keys
        while not fake_terminal.in_cbreak:
            await asyncio.sleep(0.005)


async def test_keys_arrive_and_the_mode_is_left_after(
    fake_terminal: type[FakeTerminal],
) -> None:
    fake_terminal.typed += [
        Keystroke("a"),
        Keystroke("\x1b[A", name="KEY_UP"),
        Keystroke("\r", name="KEY_ENTER"),
    ]
    pressed: list[str] = []

    async with read_keys(pressed.append) as reading:
        assert reading
        await in_cbreak_mode(fake_terminal)
        assert fake_terminal.in_cbreak == ["entered"]
        await asyncio.sleep(0.1)

    assert fake_terminal.in_cbreak == ["entered", "left"]
    assert pressed == ["a", ENTER]


async def test_ctrl_c_read_as_a_key_stays_a_signal(
    fake_terminal: type[FakeTerminal], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_terminal.typed.append(Keystroke("\x03", name="KEY_CTRL_C"))
    raised: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", raised.append)

    async with read_keys(lambda key: pytest.fail(f"pressed {key!r}")):
        await asyncio.sleep(0.1)

    assert raised == [signal.SIGINT]


async def test_restoring_for_a_quit_then_leaving_restores_once(
    fake_terminal: type[FakeTerminal],
) -> None:
    async with read_keys(lambda _key: None):
        await in_cbreak_mode(fake_terminal)
        restore_terminal()
        assert fake_terminal.in_cbreak == ["entered", "left"]

    assert fake_terminal.in_cbreak == ["entered", "left"]
    restore_terminal()


async def test_a_terminal_slow_to_answer_holds_up_nothing(
    fake_terminal: type[FakeTerminal],
) -> None:
    answers = fake_terminal.answering = threading.Event()

    async with read_keys(lambda _key: None) as reading:
        # The event loop is here, while the terminal hasn't answered.
        assert reading
        await asyncio.sleep(0.05)
        assert fake_terminal.in_cbreak == []
        answers.set()
        await in_cbreak_mode(fake_terminal)

    assert fake_terminal.in_cbreak == ["entered", "left"]


async def test_leaving_before_the_terminal_answers_never_changes_its_mode(
    fake_terminal: type[FakeTerminal],
) -> None:
    answers = fake_terminal.answering = threading.Event()

    async with read_keys(lambda _key: None):
        await asyncio.sleep(0.01)

    answers.set()
    await asyncio.sleep(0.1)
    assert fake_terminal.in_cbreak == []


async def test_keys_are_not_read_unless_stdin_is_a_terminal(
    fake_terminal: type[FakeTerminal], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TerminalStdin, "isatty", lambda _self: False)

    async with read_keys(lambda _key: None) as reading:
        assert not reading

    assert fake_terminal.in_cbreak == []


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX process group")
async def test_keys_are_not_read_from_the_background(
    fake_terminal: type[FakeTerminal], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "tcgetpgrp", lambda _fd: os.getpgrp() + 1)

    async with read_keys(lambda _key: None) as reading:
        assert not reading

    assert fake_terminal.in_cbreak == []


# Run in a real terminal, with stdout a pipe, as `ezhpcy tunnel | tee` has it.
_READING_KEYS = """
import asyncio, fcntl, os, signal, sys, termios
from ezhpcy.cli.utils.keys import read_keys, restore_terminal

# As a login shell's child: the terminal is its controlling one, in the foreground.
fcntl.ioctl(0, termios.TIOCSCTTY, 0)

def say(*what):
    print(*what, flush=True)

def on_key(key):
    say("key", repr(key))
    if key == "f":
        restore_terminal()
        say("force-quit")
        os._exit(130)
    if key == "q":
        stopping.set()

async def main():
    global stopping
    stopping = asyncio.Event()
    signal.signal(signal.SIGINT, lambda *_: say("interrupted"))
    async with read_keys(on_key) as reading:
        say("reading", reading)
        await stopping.wait()

asyncio.run(main())
"""


_READY_KEY = "key '.'"
# Python, importing the CLI, which is slow on some filesystems.
_START_SECONDS = 60


class TerminalChild:
    """A process that reads keys, with a pseudo-terminal of its own."""

    def __init__(self) -> None:
        import pty
        import termios

        self.termios = termios
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(
            [sys.executable, "-c", _READING_KEYS],
            stdin=slave,
            stderr=slave,
            stdout=subprocess.PIPE,
            env={
                **os.environ,
                "TERM": "xterm-256color",
                # This suite's own, however it found it.
                "PYTHONPATH": str(Path(ezhpcy.__file__).parents[1]),
            },
            start_new_session=True,
            text=True,
        )
        os.close(slave)
        self.shown = b""
        # Through a thread: lines read together would be in a buffer that `select`
        # on the pipe can't see.
        self.lines: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        # What the terminal shows, which would fill it up if nobody read it, and
        # the answer to where its cursor is, as any real one gives.
        with suppress(OSError):
            while data := os.read(self.master, 4096):
                self.shown += data
                if b"\x1b[6n" in data:
                    os.write(self.master, b"\x1b[1;1R")

    def _read_lines(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line.strip())

    def _line(self, seconds: float) -> str | None:
        try:
            return self.lines.get(timeout=seconds)
        except queue.Empty:
            return None

    def said(self) -> str:
        """What it says next, but for the keys `reading` pressed."""
        while True:
            line = self._line(WAIT_SECONDS)
            assert line is not None, f"it said nothing, and showed: {self.shown!r}"
            if line != _READY_KEY:
                return line

    def press(self, keys_pressed: bytes) -> None:
        os.write(self.master, keys_pressed)

    def local_flags(self) -> int:
        return self.termios.tcgetattr(self.master)[3]

    def reading(self) -> None:
        """
        Wait until keys reach it. Its mode doesn't say: blessed is in cbreak mode
        while it asks the terminal what it can do too, and loses what is typed then.
        """
        line = self._line(_START_SECONDS)
        assert line == "reading True", f"it said {line!r}, and showed: {self.shown!r}"
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            self.press(b".")
            if self._line(0.1) == _READY_KEY:
                return
            assert time.monotonic() < deadline, f"no keys: {self.shown!r}"

    def in_cooked_mode(self) -> bool:
        flags = self.local_flags()
        return bool(flags & self.termios.ICANON and flags & self.termios.ECHO)

    def close(self) -> None:
        self.process.kill()
        self.process.wait()
        self._reader.join(WAIT_SECONDS)
        assert self.process.stdout is not None
        self.process.stdout.close()
        os.close(self.master)


@pytest.fixture
def terminal_child() -> Iterator[TerminalChild]:
    child = TerminalChild()
    yield child
    child.close()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX pseudo-terminal")
def test_reading_keys_in_a_real_terminal(terminal_child: TerminalChild) -> None:
    child = terminal_child
    termios = child.termios
    child.reading()
    flags = child.local_flags()
    assert not flags & termios.ICANON
    assert not flags & termios.ECHO
    assert flags & termios.ISIG

    child.press("xå\x1b[Ay\r\x7f".encode())
    assert [child.said() for _ in range(5)] == [
        "key 'x'",
        "key 'å'",
        "key 'y'",
        f"key {ENTER!r}",
        f"key {BACKSPACE!r}",
    ]
    child.press(b"\x03")
    assert child.said() == "interrupted"
    child.press(b"q")
    assert child.said() == "key 'q'"

    assert child.process.wait(WAIT_SECONDS) == 0
    assert child.in_cooked_mode()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX pseudo-terminal")
def test_quitting_at_once_gives_the_terminal_back(
    terminal_child: TerminalChild,
) -> None:
    terminal_child.reading()

    terminal_child.press(b"f")

    assert terminal_child.said() == "key 'f'"
    assert terminal_child.said() == "force-quit"
    assert terminal_child.process.wait(WAIT_SECONDS) == 130
    assert terminal_child.in_cooked_mode()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX signal")
def test_resuming_sets_the_mode_again(terminal_child: TerminalChild) -> None:
    termios = terminal_child.termios
    terminal_child.reading()
    attributes = termios.tcgetattr(terminal_child.master)
    # As bash's `fg` leaves it.
    attributes[3] |= termios.ICANON | termios.ECHO
    termios.tcsetattr(terminal_child.master, termios.TCSANOW, attributes)
    assert terminal_child.in_cooked_mode()

    terminal_child.process.send_signal(signal.SIGCONT)

    deadline = time.monotonic() + WAIT_SECONDS
    while terminal_child.in_cooked_mode():
        assert time.monotonic() < deadline, "still in its normal mode"
        time.sleep(0.01)
    terminal_child.press(b"q")
    assert terminal_child.said() == "key 'q'"
    assert terminal_child.process.wait(WAIT_SECONDS) == 0
    assert terminal_child.in_cooked_mode()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX signal")
def test_being_terminated_gives_the_terminal_back(
    terminal_child: TerminalChild,
) -> None:
    terminal_child.reading()

    terminal_child.process.terminate()

    assert terminal_child.process.wait(WAIT_SECONDS) == -signal.SIGTERM
    assert terminal_child.in_cooked_mode()


def test_actions_get_keys_in_order_and_timers_get_none() -> None:
    menu, _running = menu_of(
        first=action(),
        timed=action(trigger={"manual": False, "every": 60}),
        both=action(trigger={"every": 60}),
    )

    assert [(entry.name, entry.key) for entry in menu.entries] == [
        ("first", "1"),
        ("timed", None),
        ("both", "2"),
    ]
    assert menu.has_keys


def test_a_timer_alone_offers_no_keys() -> None:
    menu, _running = menu_of(timed=action(trigger={"manual": False, "every": 60}))

    assert not menu.has_keys


def test_there_may_be_more_actions_than_keys() -> None:
    menu, _running = menu_of(**{f"a{index}": action() for index in range(40)})

    assert sum(entry.key is not None for entry in menu.entries) == 35
    assert menu.entries[9].key == "a"


async def test_a_key_runs_an_action_without_inputs() -> None:
    menu, running = menu_of(go=action())

    await press(menu, "1")

    assert running.ran == [("go", {})]
    assert menu.prompt == ()


async def test_a_key_nothing_has_does_nothing() -> None:
    menu, running = menu_of(go=action())

    await press(menu, "2", "x", ENTER, ESCAPE)

    assert running.ran == []


async def test_inputs_are_asked_for_in_the_panel_one_at_a_time() -> None:
    menu, running = menu_of(
        go=action(
            run={"command": ["{{ amount }}", "{{ note }}"]},
            input={
                "amount": amount_input(label="How many"),
                "note": {"type": "string"},
            },
        )
    )

    await press(menu, "1")
    assert running.ran == []
    assert "go: How many (integer, 1 to 10): █" in shown(menu)
    assert "Enter accepts, Esc cancels." in shown(menu)

    await press(menu, "12", BACKSPACE, BACKSPACE, "7")
    assert "go: How many (integer, 1 to 10): 7█" in shown(menu)
    await press(menu, ENTER)
    assert "go: note (text): █" in shown(menu)
    # Keys that are other actions' are text here.
    await press(menu, "1 b", ENTER)

    assert running.ran == [("go", {"amount": 7, "note": "1 b"})]
    assert menu.prompt == ()


async def test_a_wrong_value_is_said_and_can_be_corrected() -> None:
    menu, running = menu_of(
        go=action(run={"command": ["{{ amount }}"]}, input={"amount": amount_input()})
    )

    await press(menu, "1", "x", ENTER)
    assert "'x' is not an integer." in " ".join(shown(menu))
    await press(menu, BACKSPACE)
    assert "not an integer" not in " ".join(shown(menu))
    await press(menu, "11", ENTER)
    assert "more than the maximum" in " ".join(shown(menu))
    await press(menu, BACKSPACE, ENTER)

    assert running.ran == [("go", {"amount": 1})]


async def test_enter_takes_the_default_and_nothing_is_no_value_without_one() -> None:
    menu, running = menu_of(
        go=action(
            run={"command": ["{{ a }}", "{{ b }}"]},
            input={"a": amount_input(default=3), "b": amount_input()},
        )
    )

    await press(menu, "1", ENTER)
    assert "b (integer, 1 to 10): █" in " ".join(shown(menu))
    await press(menu, ENTER)
    assert "It needs a value." in " ".join(shown(menu))
    await press(menu, "2", ENTER)

    assert running.ran == [("go", {"a": 3, "b": 2})]


async def test_esc_cancels_asking() -> None:
    menu, running = menu_of(
        go=action(run={"command": ["{{ amount }}"]}, input={"amount": amount_input()})
    )

    await press(menu, "1", "5", ESCAPE)

    assert menu.prompt == ()
    assert running.ran == []


@pytest.mark.usefixtures("quiet_api")
async def test_a_running_action_or_a_stopped_app_runs_nothing_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    menu, running = menu_of(go=action())
    running.runs = [api.ActionRun(action="go", started=NOW)]

    await press(menu, "1")

    running.runs = []
    running.state = api.AppState.EXITED
    await press(menu, "1")

    assert running.ran == []
    assert [record.getMessage() for record in caplog.records] == [
        "Action go is running already.",
        "Action go needs app lab v1.0 running, and it isn't.",
    ]


async def test_closing_takes_no_more_keys_and_stops_asking() -> None:
    menu, running = menu_of(
        go=action(run={"command": ["{{ amount }}"]}, input={"amount": amount_input()})
    )
    await press(menu, "1")

    menu.close()
    await press(menu, "1", ENTER)

    assert menu.prompt == ()
    assert running.ran == []


async def test_stopping_cancels_the_runs_it_began() -> None:
    menu, running = menu_of(go=action())
    cancelled = asyncio.Event()

    async def run_action(_name: str, /, **_inputs: object) -> api.ActionRun:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError

    running.run_action = run_action  # pyrefly: ignore[bad-assignment]
    await press(menu, "1")

    await menu.stop()

    assert cancelled.is_set()


@pytest.mark.parametrize(
    ("fields", "words"),
    [
        ({"type": "string"}, "text"),
        ({"type": "string", "default": "x"}, "text, default x"),
        ({"type": "integer"}, "integer"),
        ({"type": "integer", "min": 1}, "integer, at least 1"),
        ({"type": "number", "max": 1.5}, "number, at most 1.5"),
        ({"type": "number", "min": 0, "max": 1}, "number, 0.0 to 1.0"),
        ({"type": "boolean", "default": True}, "yes or no, default True"),
        ({"type": "choice", "choices": ("a", "b")}, "a or b"),
        ({"type": "path"}, "path on the host"),
        (
            {"type": "path", "kind": "directory", "must_exist": True},
            "directory on the host, which must exist",
        ),
    ],
)
def test_what_an_input_takes_is_described(
    fields: dict[str, object], words: str
) -> None:
    assert describe(TypeAdapter(api.Parameter).validate_python(fields)) == words


def test_the_panel_shows_actions_their_keys_and_latest_runs() -> None:
    menu, running = menu_of(
        never=action(description="Never run"),
        going=action(),
        fine=action(),
        broken=action(),
        cut_off=action(),
        timed=action(trigger={"manual": False, "every": timedelta(hours=1)}),
    )
    running.runs = [
        api.ActionRun(action="going", started=NOW - 5),
        api.ActionRun(
            action="fine",
            state=api.ActionState.SUCCEEDED,
            started=NOW - 90,
            finished=NOW - 60,
        ),
        api.ActionRun(
            action="broken",
            state=api.ActionState.FAILED,
            started=NOW - 9,
            finished=NOW - 3,
            exit_status=2,
        ),
        api.ActionRun(
            action="cut_off",
            state=api.ActionState.FAILED,
            started=NOW - 9,
            finished=NOW - 3,
        ),
    ]

    lines = shown(menu)

    assert "1 never Never run" in lines
    assert "2 going running (5 s)" in lines
    assert "3 fine succeeded (1 min 0 s ago)" in lines
    assert "4 broken failed with status 2 (3 s ago)" in lines
    assert "5 cut_off failed (3 s ago)" in lines
    assert "timed (every 1 h 0 min)" in lines
    assert "Press an action's key to run it, or Ctrl+C to stop." in lines


def test_the_panel_shows_a_result_of_one_line_as_it_is() -> None:
    menu, running = menu_of(short=action(), long=action())
    finished = {
        "state": api.ActionState.SUCCEEDED,
        "started": NOW - 9,
        "finished": NOW - 3,
    }
    running.runs = [
        api.ActionRun(action="short", stdout="19.4/30.0G [18:04]\n", **finished),
        api.ActionRun(action="long", stdout="one\ntwo\n", **finished),
    ]

    lines = shown(menu)

    assert "1 short 19.4/30.0G [18:04] (3 s ago)" in lines
    assert "2 long succeeded (3 s ago)" in lines


def test_the_panel_says_an_app_of_actions_alone_is_ready() -> None:
    menu, _running = menu_of(go=action())

    assert "lab v1.0 ready" in shown(menu)


def test_the_panel_shows_no_actions_of_an_app_that_doesnt_run() -> None:
    menu, running = menu_of(go=action())
    running.state = api.AppState.EXITED

    assert not any(line.startswith("1 go") for line in shown(menu))


def test_the_panel_without_keys_says_only_how_to_stop() -> None:
    menu, _running = menu_of(timed=action(trigger={"manual": False, "every": 60}))

    assert "Press Ctrl+C to stop." in shown(menu)


def test_while_stopping_the_panel_says_how_to_force_quit() -> None:
    menu, running = menu_of(go=action())
    job = replace(SERVING, phase=api.Phase.STOPPING, attachments=(running.status,))
    stream = io.StringIO()
    panel = status_panel(job, title=TITLE, login=LOGIN, now=NOW, menu=menu)
    Console(file=stream, width=120, color_system=None).print(panel)

    assert "(press Ctrl+C again to force quit)" in stream.getvalue()


@pytest.mark.usefixtures("quiet_api")
async def test_a_run_says_how_it_ended_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    menu, running = menu_of(go=action())

    with caplog.at_level("INFO", logger="ezhpcy"):
        running.result = api.ActionRun(
            action="go",
            state=api.ActionState.SUCCEEDED,
            started=NOW,
            finished=NOW + 2,
            exit_status=0,
            stdout="one\ntwo\n",
        )
        await press(menu, "1")
        running.result = api.ActionRun(
            action="go",
            state=api.ActionState.FAILED,
            started=NOW,
            finished=NOW + 2,
            exit_status=3,
            stdout="partly\n",
            stderr="it broke\n",
        )
        await press(menu, "1")
        running.result = api.ActionError(t"nope")
        await press(menu, "1")

    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("INFO", "Action go of app lab v1.0 is running."),
        ("INFO", "Action go of app lab v1.0 succeeded in 2 s."),
        ("INFO", "  one"),
        ("INFO", "  two"),
        ("INFO", "Action go of app lab v1.0 is running."),
        ("WARNING", "Action go of app lab v1.0 exited with status 3 after 2 s."),
        ("INFO", "  partly"),
        ("WARNING", "  it broke"),
        ("INFO", "Action go of app lab v1.0 is running."),
        ("WARNING", "Action go of app lab v1.0 could not run: nope"),
    ]


def test_a_url_goes_above_the_live_display_on_one_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = io.StringIO()
    use_terminal(monkeypatch, terminal)
    stdout = make_terminal_console(terminal)
    monkeypatch.setattr(consoles, "console", stdout)

    with consoles.live_display(lambda: "panel"):
        consoles.print_for_scripts("https://example.com/")
        consoles.flush_output()
        written = terminal.getvalue()

    # As the display's console writes: over the display, which it draws again
    # after, rather than straight after the display's last line.
    assert "https://example.com/\n" in written
    assert "panelhttps" not in written
    assert written.rindex("panel") > written.index("https://example.com/")


def make_terminal_console(stream: io.StringIO) -> Console:
    return consoles.make_console(
        file=stream, force_terminal=True, color_system=None, width=120
    )


def test_a_url_on_stdout_of_a_pipe_goes_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = io.StringIO()
    use_terminal(monkeypatch, terminal)
    piped = io.StringIO()
    monkeypatch.setattr(consoles, "console", consoles.make_console(file=piped))

    with consoles.live_display(lambda: "panel"):
        consoles.print_for_scripts("https://example.com/")
        consoles.flush_output()

    assert piped.getvalue() == "https://example.com/\n"
    assert "example.com" not in terminal.getvalue()


def test_a_redraw_can_be_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    use_terminal(monkeypatch, io.StringIO())
    draws: list[float] = []

    def count() -> str:
        draws.append(time.monotonic())
        return "panel"

    with consoles.live_display(count):
        consoles.flush_output()
        before = len(draws)
        consoles.redraw_live_display()
        consoles.flush_output()
        assert len(draws) > before
