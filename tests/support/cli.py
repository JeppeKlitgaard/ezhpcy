"""Run the EzHPCy CLI in-process, independently of the CLI framework behind it."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import BinaryIO, Literal

import keyring
import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app, provision, proxy, prune, tunnel
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
    result = CliRunner().invoke(
        app,
        list(args),
        # Rich takes its width from COLUMNS, not from Click's terminal width.
        env={"COLUMNS": str(_TERMINAL_WIDTH), **(env or {})},
        input=input,
        color=color,
        terminal_width=_TERMINAL_WIDTH,
    )
    exception = result.exception
    if isinstance(exception, SystemExit):
        exception = None
    return Result(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        exception=exception,
    )


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
