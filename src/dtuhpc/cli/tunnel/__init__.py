import typer

from dtuhpc.cli.tunnel.interactive import interactive_cmd
from dtuhpc.patch.typer_alias import AliasGroup

app = typer.Typer(
    cls=AliasGroup,
    help="Open tunnels for DTU HPC workflows.",
    no_args_is_help=True,
)


def _batch() -> None:
    """Run DTU HPC batch workflow."""
    typer.echo("Running batch workflow.")


app.command(name="interactive, i", help="Start an interactive session.")(interactive_cmd)
app.command(name="batch, b", help="Run a batch workflow.")(_batch)
