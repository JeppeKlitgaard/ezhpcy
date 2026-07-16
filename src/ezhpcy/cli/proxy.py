import sys

import typer

from ezhpcy.cli.common import (
    ProfileContext,
    with_profile_context,
)
from ezhpcy.ipc import load_broker_backend
from ezhpcy.ipc.common import IPCError
from ezhpcy.tunnel.broker import relay_proxy_stdio


@with_profile_context
def proxy_cmd(
    profile_context: ProfileContext,
) -> None:
    """Relay ProxyCommand stdin/stdout through a running tunnel broker."""
    try:
        relay_proxy_stdio(
            load_broker_backend(
                profile=profile_context.name,
                resolved_config=(
                    profile_context.profile if profile_context.name is None else None
                ),
            ),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    except (IPCError, EOFError, OSError) as error:
        typer.echo(f"ezhpcy proxy: {error}", err=True)
        raise typer.Exit(code=1) from error
