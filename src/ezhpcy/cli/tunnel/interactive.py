from typing import Annotated

import typer

from ezhpcy.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
    local_machine_or_fail,
)
from ezhpcy.config import config


def _shell_help() -> str:
    shells = ", ".join(config.hpc.interactive_shells)
    return f"Interactive shell command to run. Examples: {shells}."


def interactive_cmd(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.host,
    shell: Annotated[
        str | None,
        typer.Argument(
            metavar="SHELL",
            help=_shell_help(),
        ),
    ] = None,
) -> None:
    """Start an interactive HPC session."""
    connection_info_from_options(user=user, password=password, host=host)

    local_machine_or_fail()

    shell = shell or config.hpc.default_interactive_shell
    typer.echo(f"Starting interactive session with {shell}.")
