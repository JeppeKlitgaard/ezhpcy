from typing import Annotated

import typer

from ezhpcy.cli.tunnel.common import ProfileContext, with_profile_options
from ezhpcy.cli.tunnel.install import install_cmd
from ezhpcy.cli.tunnel.uninstall import uninstall_cmd


@with_profile_options
def reinstall_cmd(
    profile_context: ProfileContext,
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
        profile=profile_context.name,
        user=profile_context.connection.user,
        password=profile_context.connection.password,
        host=str(profile_context.connection.host),
        yes=yes,
    )
    install_cmd(
        profile=profile_context.name,
        user=profile_context.connection.user,
        password=profile_context.connection.password,
        host=str(profile_context.connection.host),
        yes=yes,
    )
