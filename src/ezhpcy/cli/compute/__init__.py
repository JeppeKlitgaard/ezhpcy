import typer

from ezhpcy.cli.compute.ssh_serve import ssh_serve_cmd
from ezhpcy.cli.utils.alias import AliasGroup

compute_app = typer.Typer(
    cls=AliasGroup,
    help="Commands to be executed at the compute node of the HPC.",
    no_args_is_help=True,
)

compute_app.command(name="ssh-serve", help="Start an SSH server.")(ssh_serve_cmd)
