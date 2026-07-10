import typer

from ezhpcy.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
)
from ezhpcy.cli.tunnel.interactive import interactive_cmd
from ezhpcy.cli.tunnel.install import install_cmd
from ezhpcy.config import config
from ezhpcy.cli.utils.alias import AliasGroup

tunnel_app = typer.Typer(
    cls=AliasGroup,
    help="Open tunnels for HPC workflows.",
    no_args_is_help=True,
)


def _batch(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.host,
) -> None:
    """Run HPC batch workflow."""
    connection_info_from_options(user=user, password=password, host=host)
    typer.echo("Running batch workflow.")


tunnel_app.command(name="install", help="Install ezhpcy on HPC.")(install_cmd)
tunnel_app.command(name="interactive, i", help="Start an interactive session.")(
    interactive_cmd
)
tunnel_app.command(name="batch, b", help="Run a batch workflow.")(_batch)
