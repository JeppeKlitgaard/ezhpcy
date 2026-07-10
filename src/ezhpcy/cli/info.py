import typer

from ezhpcy.cli.version import ezhpcy_version
from ezhpcy.detect import get_host_type, get_scheduler_type


def info_cmd() -> None:
    """Show debug information for ezhpcy."""
    typer.echo(f"EZHPCY Version: {ezhpcy_version()}")
    typer.echo(f"Host Type: {get_host_type().value}")
    typer.echo(f"Scheduler Type: {get_scheduler_type().value}")
