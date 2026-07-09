import typer

from dtuhpc.cli.version import dtuhpc_version
from dtuhpc.detect import get_host_type, get_scheduler_type


def info_cmd() -> None:
    """Show debug information for dtuhpc."""
    typer.echo(f"DTUHPC Version: {dtuhpc_version()}")
    typer.echo(f"Host Type: {get_host_type().value}")
    typer.echo(f"Scheduler Type: {get_scheduler_type().value}")
