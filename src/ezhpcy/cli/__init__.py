import typer

from ezhpcy.cli.compute import compute_app
from ezhpcy.cli.info import info_cmd
from ezhpcy.cli.proxy import proxy_cmd
from ezhpcy.cli.tunnel import tunnel_app
from ezhpcy.cli.utils.alias import AliasGroup
from ezhpcy.cli.version import version_cmd

app = typer.Typer(
    cls=AliasGroup,
    help="Utilities for working with HPC facilities.",
    no_args_is_help=True,
)

app.add_typer(tunnel_app, name="tunnel, t")
app.add_typer(compute_app, name="compute")
app.command(name="info", help="Show debug information.")(info_cmd)
app.command(name="version", help="Show the ezhpcy version.")(version_cmd)
app.command(name="proxy", help="Proxy SSH bytes through a running tunnel broker.")(
    proxy_cmd
)
