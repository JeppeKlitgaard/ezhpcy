from typing import Annotated

import typer

from dtuhpc.config import ConnectionInfo, config
from dtuhpc.detect import HostType, get_host_type
from dtuhpc.patch.typer_resolve import resolve_forbidden_none

UserOpt = Annotated[
    str | None, typer.Option("--user", "-u", help="Username for the login node.")
]
PasswordOpt = Annotated[
    str | None, typer.Option("--password", "-p", help="Password for the login node.")
]
HostOpt = Annotated[
    str | None, typer.Option("--host", "-h", help="Login node address.")
]


def connection_info_from_options(
    *,
    user: str | None,
    password: str | None,
    host: str | None,
) -> ConnectionInfo:
    user = resolve_forbidden_none(
        cli_value=user,
        config_value=config.connection.user,
        name="user",
        cli_param="--user",
        config_param="connection.user",
    )
    return ConnectionInfo(
        user=user,
        password=password,
        login_node_address=host or config.connection.login_node_address,
    )


def local_machine_or_fail() -> None:
    """
    Fails the command if the current HostType is not OTHER.
    """
    host_type = get_host_type()
    if host_type != HostType.OTHER:
        print(
            f"This command should be run on your local machine (detected: {host_type.value})."
        )
        raise typer.Exit(code=1)
