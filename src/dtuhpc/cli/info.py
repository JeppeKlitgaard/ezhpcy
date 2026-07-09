from importlib.metadata import PackageNotFoundError, version

import typer

from dtuhpc.detect import get_host_type, get_scheduler_type


def _dtuhpc_version() -> str:
    try:
        return version("dtuhpc")
    except PackageNotFoundError:
        return "unknown"


def info_cmd() -> None:
    """Show debug information for dtuhpc."""
    typer.echo(f"DTUHPC Version: {_dtuhpc_version()}")
    typer.echo(f"Host Type: {get_host_type().value}")
    typer.echo(f"Scheduler Type: {get_scheduler_type().value}")
