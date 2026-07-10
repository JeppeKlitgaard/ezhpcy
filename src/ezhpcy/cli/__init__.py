import typer

from ezhpcy.cli.info import info_cmd
from ezhpcy.cli.tunnel import tunnel_app
from ezhpcy.cli.version import version_cmd
from ezhpcy.patch.typer_alias import AliasGroup

app = typer.Typer(
    cls=AliasGroup,
    help="Utilities for working with DTU HPC.",
    no_args_is_help=True,
)

app.add_typer(tunnel_app, name="tunnel, t")
app.command(name="info", help="Show debug information.")(info_cmd)
app.command(name="version", help="Show the ezhpcy version.")(version_cmd)
