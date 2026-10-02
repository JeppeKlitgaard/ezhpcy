"""Run the EzHPCy CLI in-process, independently of the CLI framework behind it."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from typer.testing import CliRunner

from ezhpcy.cli import app

# Wide enough that help and error panels don't wrap the text tests look for.
_TERMINAL_WIDTH = 160


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
        env=env,
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
