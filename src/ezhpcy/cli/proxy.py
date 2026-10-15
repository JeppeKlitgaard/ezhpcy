lazy import logging
lazy import sys

lazy from ezhpcy.cli._options import AliasArg
lazy from ezhpcy.console import error_console
lazy from ezhpcy.ipc import IPCError
lazy from ezhpcy.ipc.client import connect, relay_proxy_stdio
lazy from ezhpcy.ipc.descriptor import TunnelDescriptor
lazy from ezhpcy.logging import configure_logging


def proxy_cmd(alias: AliasArg, /) -> None:
    """Relay ProxyCommand stdin/stdout through a running tunnel."""
    try:
        descriptor = TunnelDescriptor.load(alias)
        if descriptor.debug:
            # OpenSSH starts this process from a fixed ProxyCommand line, so the
            # tunnel's own --debug reaches it through the tunnel's descriptor.
            # Logging is stderr-only; stdout carries SSH bytes exclusively.
            configure_logging(logging.DEBUG, include_timestamp=True)
        with connect(descriptor) as stream:
            relay_proxy_stdio(stream, sys.stdin.buffer, sys.stdout.buffer)
    except ValueError as error:
        error_console.print(f"ezhpcy proxy: {error}", markup=False)
        raise SystemExit(2) from error
    except (IPCError, OSError) as error:
        error_console.print(f"ezhpcy proxy: {error}", markup=False)
        raise SystemExit(1) from error
