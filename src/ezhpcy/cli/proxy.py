import logging
import sys

from ezhpcy.cli._options import AliasArg
from ezhpcy.console import error_console
from ezhpcy.ipc import load_tunnel_backend
from ezhpcy.ipc.common import IPCError
from ezhpcy.logging import configure_logging
from ezhpcy.tunnel.server import relay_proxy_stdio


def proxy_cmd(alias: AliasArg, /) -> None:
    """Relay ProxyCommand stdin/stdout through a running tunnel."""
    try:
        backend = load_tunnel_backend(alias)
        if backend.debug:
            # OpenSSH starts this process from a fixed ProxyCommand line, so the
            # tunnel's own --debug reaches it through the tunnel's descriptor.
            # Logging is stderr-only; stdout carries SSH bytes exclusively.
            configure_logging(logging.DEBUG, include_timestamp=True)
        relay_proxy_stdio(
            backend,
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    except ValueError as error:
        error_console.print(f"ezhpcy proxy: {error}", markup=False)
        raise SystemExit(2) from error
    except (IPCError, EOFError, OSError) as error:
        error_console.print(f"ezhpcy proxy: {error}", markup=False)
        raise SystemExit(1) from error
