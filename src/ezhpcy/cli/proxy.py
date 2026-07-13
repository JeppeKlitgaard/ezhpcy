import sys

import typer

from ezhpcy.tunnel.broker import relay_proxy_stdio
from ezhpcy.tunnel.ipc import IPCError, load_broker_backend


def proxy_cmd() -> None:
    """Relay ProxyCommand stdin/stdout through a running tunnel broker."""
    try:
        relay_proxy_stdio(load_broker_backend(), sys.stdin.buffer, sys.stdout.buffer)
    except (IPCError, EOFError, OSError) as error:
        typer.echo(f"ezhpcy proxy: {error}", err=True)
        raise typer.Exit(code=1) from error
