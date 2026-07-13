from typing import Annotated

import typer

from ezhpcy.cli.tunnel.common import with_connection_options
from ezhpcy.cli.tunnel.install import install_cmd
from ezhpcy.cli.tunnel.uninstall import uninstall_cmd
from ezhpcy.config import ConnectionInfo


@with_connection_options
def reinstall_cmd(
    conn_info: ConnectionInfo,
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
    # Resolve one-shot sources such as file descriptors and keyrings once, then
    # reuse the resulting credentials for both halves of the operation.
    uninstall_cmd(
        user=conn_info.user,
        password=conn_info.password,
        host=str(conn_info.host),
        yes=yes,
    )
    install_cmd(
        user=conn_info.user,
        password=conn_info.password,
        host=str(conn_info.host),
        yes=yes,
    )
