import typer

from ezhpcy.cli.config.edit import edit_cmd
from ezhpcy.cli.utils.alias import AliasGroup

config_app = typer.Typer(
    cls=AliasGroup,
    help="Manage the ezhpcy configuration file.",
    no_args_is_help=True,
)

config_app.command(name="edit", help="Open the configuration file in an editor.")(
    edit_cmd
)
