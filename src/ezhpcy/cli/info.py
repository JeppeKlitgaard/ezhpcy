import typer

from ezhpcy.cli.version import ezhpcy_version
from ezhpcy.detect.host_type import get_host_type
from ezhpcy.detect.scheduler_type import get_scheduler_type


def info_cmd() -> None:
    """Show debug information for ezhpcy."""
    typer.echo(f"EZHPCY Version: {ezhpcy_version()}")
    typer.echo(f"Host Type: {get_host_type().value}")
    scheduler_type = get_scheduler_type()
    scheduler_name = scheduler_type.value if scheduler_type is not None else "UNKNOWN"
    typer.echo(f"Scheduler Type: {scheduler_name}")
