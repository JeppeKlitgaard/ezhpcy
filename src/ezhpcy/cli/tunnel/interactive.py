from typing import Annotated

import typer

from ezhpcy.cli.tunnel.common import local_machine_or_fail, with_connection_options
from ezhpcy.config import ConnectionInfo, config


def _shell_help() -> str:
    shells = ", ".join(config.hpc.interactive_shells)
    return f"Interactive shell command to run. Examples: {shells}."


@with_connection_options
def interactive_cmd(
    conn_info: ConnectionInfo,
    shell: Annotated[
        str | None,
        typer.Argument(
            metavar="SHELL",
            help=_shell_help(),
        ),
    ] = None,
) -> None:
    """Start an interactive HPC session."""
    local_machine_or_fail()

    shell = shell or config.hpc.default_interactive_shell
    typer.echo(f"Starting interactive session with {shell}.")
