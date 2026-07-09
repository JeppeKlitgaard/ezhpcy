from typing import Annotated

import typer

from dtuhpc.cli.tunnel.common import local_machine_or_fail
from dtuhpc.config import config


def _shell_help() -> str:
    shells = ", ".join(config.hpc.interactive_shells)
    return f"Interactive shell command to run. Examples: {shells}."


def interactive_cmd(
    shell: Annotated[
        str | None,
        typer.Argument(
            metavar="SHELL",
            help=_shell_help(),
        ),
    ] = None,
) -> None:
    """Start an interactive DTU HPC session."""
    local_machine_or_fail()

    shell = shell or config.hpc.default_interactive_shell
    typer.echo(f"Starting interactive session with {shell}.")
