import typer

from dtuhpc.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
)
from dtuhpc.cli.tunnel.interactive import interactive_cmd
from dtuhpc.config import config
from dtuhpc.patch.typer_alias import AliasGroup

tunnel_app = typer.Typer(
    cls=AliasGroup,
    help="Open tunnels for DTU HPC workflows.",
    no_args_is_help=True,
)


def _batch(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.login_node_address,
) -> None:
    """Run DTU HPC batch workflow."""
    connection_info_from_options(user=user, password=password, host=host)
    typer.echo("Running batch workflow.")


tunnel_app.command(name="interactive, i", help="Start an interactive session.")(
    interactive_cmd
)
tunnel_app.command(name="batch, b", help="Run a batch workflow.")(_batch)
