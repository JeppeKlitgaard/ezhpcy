import typer

from dtuhpc.cli.info import info_cmd
from dtuhpc.cli.tunnel import app as tunnel_app
from dtuhpc.patch.typer_alias import AliasGroup

app = typer.Typer(
    cls=AliasGroup,
    help="Utilities for working with DTU HPC.",
    no_args_is_help=True,
)

app.add_typer(tunnel_app, name="tunnel, t")
app.command(name="info", help="Show debug information.")(info_cmd)
