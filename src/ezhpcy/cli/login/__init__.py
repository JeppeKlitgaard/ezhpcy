import typer

from ezhpcy.cli.utils.alias import AliasGroup

login_app = typer.Typer(
    cls=AliasGroup,
    help="Commands to be executed at the login node of the HPC.",
    no_args_is_help=True,
)
