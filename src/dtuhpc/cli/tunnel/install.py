import typer

from dtuhpc.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
    local_machine_or_fail,
)
from dtuhpc.config import config

def install_cmd(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.login_node_address,
) -> None:
    """Start an interactive DTU HPC session."""
    connection_info_from_options(user=user, password=password, host=host)

    local_machine_or_fail()

    typer.echo("...")
