import sys

import typer

from ezhpcy.cli.common import ProfileOpt
from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.config import config
from ezhpcy.ipc import load_broker_backend
from ezhpcy.ipc.common import IPCError
from ezhpcy.tunnel.broker import relay_proxy_stdio


def proxy_cmd(profile: ProfileOpt = None) -> None:
    """Relay ProxyCommand stdin/stdout through a running tunnel broker."""
    try:
        try:
            config.resolve_profile(profile)
        except ValueError as error:
            raise RichBadParameter(str(error), param_hint="--profile") from error
        profile_name = profile or config.default_profile
        assert profile_name is not None
        relay_proxy_stdio(
            load_broker_backend(profile_name=profile_name),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    except (IPCError, EOFError, OSError) as error:
        typer.echo(f"ezhpcy proxy: {error}", err=True)
        raise typer.Exit(code=1) from error
