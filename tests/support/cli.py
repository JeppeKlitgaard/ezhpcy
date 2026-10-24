"""Run the EzHPCy CLI in-process through its `main()`."""

import io
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Self, TextIO

import keyring
import pytest
from pydantic import SecretStr
from rich.console import Console

from ezhpcy import login as login_module
from ezhpcy.cli import main, tunnel
from ezhpcy.console import make_console
from ezhpcy.types import Login, RemoteState

# Wide enough that help and error panels never wrap: at any narrower width, which
# phrases a line break splits depends on the width and the framework's layout.
_TERMINAL_WIDTH = 1000


@dataclass(frozen=True)
class Result:
    exit_code: int
    stdout: str
    stderr: str
    # An exception that escaped the command, other than a normal exit.
    exception: BaseException | None


def invoke(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    color: bool = False,
) -> Result:
    """
    Run `ezhpcy <args>` and capture its exit code and output streams.

    With `color`, output is rendered as for a colour terminal, including ANSI
    styling.
    """
    # Don't translate newlines to the platform's line ending.
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
    exit_code = 0
    exception: BaseException | None = None
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, value in {"COLUMNS": str(_TERMINAL_WIDTH), **(env or {})}.items():
            monkeypatch.setenv(name, value)
        # Prompts read `sys.stdin` and interactive job output goes to `sys.stderr`.
        monkeypatch.setattr(
            sys,
            "stdin",
            io.TextIOWrapper(io.BytesIO((stdin or "").encode()), encoding="utf-8"),
        )
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)
        try:
            main(
                list(args),
                console=_console(stdout, color=color),
                error_console=_console(stderr, color=color),
            )
        except SystemExit as error:
            # Commands exit with a status code, never `sys.exit("message")`.
            assert error.code is None or isinstance(error.code, int), error.code
            exit_code = error.code or 0
        # Report any exception that escapes the command.
        except Exception as error:  # noqa: BLE001
            exit_code = 1
            exception = error
    return Result(
        exit_code=exit_code,
        stdout=_written(stdout),
        stderr=_written(stderr),
        exception=exception,
    )


def _console(file: TextIO, *, color: bool) -> Console:
    return make_console(
        file=file,
        width=_TERMINAL_WIDTH,
        force_terminal=color,
        no_color=not color,
        color_system="standard" if color else None,
        # Render the same on every Windows console, as Cyclopts' testing docs advise.
        legacy_windows=False,
    )


def _written(stream: io.TextIOWrapper) -> str:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue().decode("utf-8", errors="replace")


type CapturedCommand = Literal["tunnel", "provision", "prune", "keyring"]


class _Reached(Exception):
    """Raised by a fake to stop a command once what it received is recorded."""


class _FakeLogin:
    """Stands in for the login connection, and stops at the first remote operation
    after `remote_state`."""

    def __init__(self, captured: dict[str, object]) -> None:
        self._captured = captured

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        pass

    async def remote_state(self) -> RemoteState:
        return RemoteState(
            cache_dir=PurePosixPath("/home/alice/.cache"),
            state_dir=PurePosixPath("/home/alice/.local/state"),
        )

    def __getattr__(self, name: str):
        # Raises when called, so it also stops `async with login.sftp()`.
        def remote_operation(*args: object, **kwargs: object):
            self._captured["remote_operation"] = (name, args, kwargs)
            raise _Reached(name)

        return remote_operation


def capture(
    monkeypatch: pytest.MonkeyPatch, command: CapturedCommand
) -> dict[str, object]:
    """
    Stop `command` at the layer below it and return what that layer received.

    The dict stays empty if the command never got there, e.g. after a usage error.

    - `tunnel`: what it would serve once its alias is reserved: the `login`,
      the `job` (a `JobConfig`), the `alias`, the `apps` and their
      `apps_root`, the `worker_ports` and the `password` given, if any. The command then exits normally, without
      connecting.
    - `provision`, `prune`: the `login` given to `ezhpcy.login.connect`, the
      `password` it would use (given, or read from the login's sources), and
      `remote_operation` (name, args, kwargs) for the first operation after
      `remote_state`, at which the command stops with an exception.
    - `keyring`: `service`, `account` and `password` given to the system keyring.
    """
    captured: dict[str, object] = {}
    match command:
        case "tunnel":

            async def serve(
                run: tunnel.TunnelRun,
                reservation: object,
                *,
                password: SecretStr | None,
                worker_ports: tuple[int, ...],
            ) -> None:
                captured.update(
                    login=run.login,
                    job=run.config,
                    alias=run.alias,
                    apps=run.apps,
                    apps_root=run.apps_root,
                    worker_ports=worker_ports,
                    password=password,
                )

            monkeypatch.setattr(tunnel, "serve", serve)
        case "provision" | "prune":

            async def connect(
                login: Login, *, password: SecretStr | None, **_options: object
            ) -> _FakeLogin:
                captured["login"] = login
                captured["password"] = password or login_module.read_password(login)
                return _FakeLogin(captured)

            monkeypatch.setattr(login_module, "connect", connect)
        case "keyring":

            def set_password(service: str, account: str, password: str) -> None:
                captured.update(service=service, account=account, password=password)

            monkeypatch.setattr(keyring, "set_password", set_password)
    return captured
