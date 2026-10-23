"""
Ports forwarded through a host's connection, attached to the host: a loopback
port on this machine to a destination the host reaches (`forward`), or a
loopback port on the host to a destination this machine reaches
(`reverse_forward`).

Closing one, early or with its host's connection, refuses new connections; open
ones end by themselves, or when the connection closes.
"""

from dataclasses import dataclass
lazy from dataclasses import replace

from ezhpcy.logging import get_logger
lazy from ezhpcy.attachments import Attachment
lazy from ezhpcy.cleanup import finish
lazy from ezhpcy.ssh import (
    Destination,
    ForwardedPort,
    RemoteForwardedPort,
    RemoteHost,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ForwardStatus:
    """
    A forwarded port: `port`, on this machine's loopback, to `destination` from
    the host; or, `reverse`, `port` on the host's loopback to `destination`
    from this machine.
    """

    destination: Destination
    port: int
    # Connections open through the port.
    connections: int = 0
    reverse: bool = False


class Forward:
    """
    A forwarded port (`Host.forward`, `Host.reverse_forward`), attached to its
    host until closed.
    """

    def __init__(self, attachment: Attachment) -> None:
        # Use `Host.forward` or `Host.reverse_forward`.
        self._attachment = attachment

    @property
    def status(self) -> ForwardStatus:
        """A snapshot of the port, replaced whole when its connections change."""
        status = self._attachment.status
        assert isinstance(status, ForwardStatus)
        return status

    @property
    def port(self) -> int:
        """The loopback port: on this machine, or, reversed, on the host."""
        return self.status.port

    @property
    def connections(self) -> int:
        """How many connections are open through the port."""
        return self.status.connections

    async def close(self) -> None:
        """Refuse new connections; open ones stay open. Once only."""
        await self._attachment.close()


async def forward(
    host: RemoteHost, destination: Destination, *, local_port: int = 0
) -> Forward:
    """
    Listen on loopback port `local_port` here (0: a free one), and forward each
    connection to `destination` from `host`.
    """
    return await _attach(host, destination, local_port=local_port, reverse=False)


async def reverse_forward(
    host: RemoteHost, remote_port: int, destination: Destination
) -> Forward:
    """
    Have `host` listen on its loopback port `remote_port` (0: a free one), and
    forward each connection to `destination` from this machine.
    """
    return await _attach(host, destination, local_port=remote_port, reverse=True)


async def _attach(
    host: RemoteHost, destination: Destination, *, local_port: int, reverse: bool
) -> Forward:
    attachment = host.attachments.attach()

    def on_connections(count: int) -> None:
        if isinstance(status := attachment.status, ForwardStatus):
            attachment.report(replace(status, connections=count))

    def opened(port: ForwardedPort | RemoteForwardedPort) -> None:
        async def close_port() -> None:
            port.close()

        attachment.push("close the forwarded port", close_port)

    try:
        if reverse:
            port = await finish(
                "listen on the host's port",
                host.forward_remote_port(
                    local_port, destination, on_connections=on_connections
                ),
                then=opened,
            )
        else:
            port = await finish(
                "listen on the local port",
                host.forward_local_port(
                    destination, local_port=local_port, on_connections=on_connections
                ),
                then=opened,
            )
    except BaseException:
        await attachment.close()
        raise
    attachment.report(
        ForwardStatus(
            destination=destination,
            port=port.port,
            connections=port.connections.count,
            reverse=reverse,
        )
    )
    logger.debug(
        t"Forwarding port: port={port.port:detail} reverse={reverse:detail} "
        t"destination={destination!s:endpoint}"
    )
    return Forward(attachment)
