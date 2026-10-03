"""Run the EzHPCy CLI in-process, independently of the CLI framework behind it."""

import io
import logging
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import BinaryIO, Literal, TextIO

import keyring
import pytest
from rich.console import Console

from ezhpcy.cli import main, provision, proxy, prune, tunnel
from ezhpcy.types import ConnectionInfo, RemoteState

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
    env: Mapping[str, str | None] | None = None,
    input: str | None = None,
    color: bool = False,
) -> Result:
    """
    Run `ezhpcy <args>` and capture its exit code and output streams.

    With `color`, output is rendered as for a colour terminal, including ANSI
    styling.
    """
    stdin = io.TextIOWrapper(io.BytesIO((input or "").encode()), encoding="utf-8")
    # Like Click's runner, don't translate newlines to the platform's line ending.
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
    exit_code = 0
    exception: BaseException | None = None
    with (
        _environment({"COLUMNS": str(_TERMINAL_WIDTH), **(env or {})}),
        _streams(stdin, stdout, stderr),
    ):
        try:
            main(
                list(args),
                console=_console(stdout, color=color),
                error_console=_console(stderr, color=color),
            )
        except SystemExit as error:
            exit_code = _exit_code(error.code)
        # Report any exception that escapes the command, as Click's runner did.
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
    return Console(
        file=file,
        width=_TERMINAL_WIDTH,
        force_terminal=color,
        no_color=not color,
        color_system="standard" if color else None,
        # Render the same on every Windows console, as Cyclopts' testing docs advise.
        legacy_windows=False,
    )


def _exit_code(code: str | int | None) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    # `sys.exit("message")` prints the message and exits with status 1.
    print(code, file=sys.stderr)
    return 1


def _written(stream: io.TextIOWrapper) -> str:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue().decode("utf-8", errors="replace")


@contextmanager
def _environment(env: Mapping[str, str | None]) -> Iterator[None]:
    """Set (or, for `None`, unset) environment variables for the duration."""
    previous = {name: os.environ.get(name) for name in env}
    try:
        _update_environment(env)
        yield
    finally:
        _update_environment(previous)


def _update_environment(env: Mapping[str, str | None]) -> None:
    for name, value in env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@contextmanager
def _streams(stdin: TextIO, stdout: TextIO, stderr: TextIO) -> Iterator[None]:
    previous = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
    try:
        yield
    finally:
        sys.stdin, sys.stdout, sys.stderr = previous


type CapturedCommand = Literal["tunnel", "provision", "prune", "proxy", "keyring"]

# What the fake relay writes to the proxy's stdout.
RELAYED_BYTES = b"relayed"


class _Reached(Exception):
    """Raised by a fake to stop a command once what it received is recorded."""


class _FakeLoginClient:
    """Records the connection, then stops at the first remote operation."""

    def __init__(
        self,
        captured: dict[str, object],
        connection: ConnectionInfo,
        *,
        password_prompt: bool,
    ):
        captured["connection"] = connection
        captured["password_prompt"] = password_prompt
        self._captured = captured

    def interactive_connect(self) -> None:
        pass

    def get_remote_state(self) -> RemoteState:
        return RemoteState(cache_dir=PurePosixPath("/home/alice/.cache"))

    def __getattr__(self, name: str):
        def remote_operation(*args: object, **kwargs: object):
            self._captured["remote_operation"] = (name, args, kwargs)
            raise _Reached(name)

        return remote_operation


def capture(
    monkeypatch: pytest.MonkeyPatch,
    command: CapturedCommand,
    *,
    tunnel_debug: bool = False,
) -> dict[str, object]:
    """
    Stop `command` at the layer below it and return what that layer received.

    The dict stays empty if the command never got there, e.g. after a usage error.

    - `tunnel`: the keyword arguments of `_run_tunnel` (`resolved`, `ssh_host`, ...).
      The command then exits normally.
    - `provision`, `prune`: `connection` and `password_prompt` given to the login
      SSH client, and `remote_operation` (name, args, kwargs) for the first
      operation after connecting, at which the command stops with an exception.
    - `proxy`: the tunnel `alias` it looked up. The relay writes `RELAYED_BYTES` to
      stdout and logs one debug record; `tunnel_debug` is the tunnel's `--debug`.
    - `keyring`: `service`, `account` and `password` given to the system keyring.
    """
    captured: dict[str, object] = {}
    match command:
        case "tunnel":
            monkeypatch.setattr(
                tunnel, "_run_tunnel", lambda **kwargs: captured.update(kwargs)
            )
        case "provision" | "prune":
            module = provision if command == "provision" else prune
            monkeypatch.setattr(
                module,
                "InteractiveSSHClient",
                lambda *args, **kwargs: _FakeLoginClient(captured, *args, **kwargs),
            )
        case "proxy":

            def load_tunnel_backend(alias: str) -> SimpleNamespace:
                captured["alias"] = alias
                return SimpleNamespace(debug=tunnel_debug)

            def relay_proxy_stdio(
                _backend: object, _stdin: BinaryIO, stdout: BinaryIO
            ) -> None:
                logging.getLogger("ezhpcy.tunnel.server").debug("Relaying.")
                stdout.write(RELAYED_BYTES)
                stdout.flush()

            monkeypatch.setattr(proxy, "load_tunnel_backend", load_tunnel_backend)
            monkeypatch.setattr(proxy, "relay_proxy_stdio", relay_proxy_stdio)
        case "keyring":

            def set_password(service: str, account: str, password: str) -> None:
                captured.update(service=service, account=account, password=password)

            monkeypatch.setattr(keyring, "set_password", set_password)
    return captured
