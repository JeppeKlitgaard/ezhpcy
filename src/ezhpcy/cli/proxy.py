import sys

import typer

from ezhpcy.cli.common import ProfileArg
from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.config import ProfilePasswordSourceError, config
from ezhpcy.ipc import load_broker_backend
from ezhpcy.ipc.common import IPCError
from ezhpcy.tunnel.broker import relay_proxy_stdio


def proxy_cmd(profile: ProfileArg) -> None:
    """Relay ProxyCommand stdin/stdout through a running tunnel broker."""
    try:
        try:
            config.resolve_profile(profile)
        except ProfilePasswordSourceError as error:
            raise RichBadParameter(error.rich_message()) from error
        except ValueError as error:
            raise RichBadParameter(str(error), param_hint="PROFILE") from error
        relay_proxy_stdio(
            load_broker_backend(profile=profile),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    except (IPCError, EOFError, OSError) as error:
        typer.echo(f"ezhpcy proxy: {error}", err=True)
        raise typer.Exit(code=1) from error
