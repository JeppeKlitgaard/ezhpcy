from typing import Annotated

import typer

from ezhpcy.cli.tunnel.common import HostOpt, PasswordOpt, UserOpt
from ezhpcy.cli.tunnel.install import install_cmd
from ezhpcy.cli.tunnel.uninstall import uninstall_cmd
from ezhpcy.config import config


def reinstall_cmd(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.host,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Proceed with uninstallation and installation without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Uninstall ezhpcy from the remote HPC host, then install it again."""
    uninstall_cmd(user=user, password=password, host=host, yes=yes)
    install_cmd(user=user, password=password, host=host, yes=yes)
