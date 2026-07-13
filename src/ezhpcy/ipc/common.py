"""Shared IPC types without transport or storage dependencies."""

import socket
from dataclasses import InitVar, dataclass
from typing import Callable, Protocol

LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class IPCAddress:
    host: str
    port: int
    allow_zero_port: InitVar[bool] = False

    def __post_init__(self, allow_zero_port: bool) -> None:
        if self.host != LOOPBACK_HOST:
            raise ValueError("broker IPC address must use IPv4 loopback")
        minimum_port = 0 if allow_zero_port else 1
        if type(self.port) is not int or not minimum_port <= self.port <= 65535:
            raise ValueError("broker IPC port is invalid")

    def as_tuple(self) -> tuple[str, int]:
        return self.host, self.port


class IPCError(Exception):
    """Base class for actionable broker IPC failures."""


class BrokerUnavailableError(IPCError):
    """The foreground broker is not accepting connections."""


class IPCAuthenticationError(IPCError):
    """The broker and proxy do not share the same capability key."""


class ProtocolError(IPCError):
    """The peer sent an invalid or unsupported IPC message."""


class IPCServer(Protocol):
    address: IPCAddress

    def serve_forever(self) -> None: ...

    def close(self) -> None: ...


class IPCBackend(Protocol):
    def listen(
        self,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None = None,
    ) -> IPCServer: ...

    def connect(self, *, timeout: float) -> socket.socket: ...
