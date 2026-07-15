"""
This module hosts the interprocess communication (IPC) protocol for the ezhpcy tunnel.

A long-lived tunnel listens for connections on a loopback socket, which short-lived proxy processes can connect to.
The tunnel forwards the requests to a target SSH server.
"""

import secrets
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ezhpcy.config import config
from ezhpcy.ipc.common import (
    LOOPBACK_HOST,
    BrokerUnavailableError,
    IPCAddress,
    IPCAuthenticationError,
    IPCError,
    IPCServer,
)
from ezhpcy.ipc.protocol import (
    AUTHENTICATION_TIMEOUT,
    AuthenticationError,
    authenticate_client,
)
from ezhpcy.ipc.runtime import (
    load_runtime_descriptor,
    publish_runtime_descriptor,
    remove_runtime_descriptor,
)
from ezhpcy.ipc.server import AuthenticatedIPCServer

_DEFAULT_BIND_ADDRESS = IPCAddress(LOOPBACK_HOST, 0, allow_zero_port=True)


@dataclass(frozen=True)
class AuthenticatedIPCBackend:
    """An authenticated TCP endpoint bound exclusively to IPv4 loopback."""

    address: IPCAddress
    authkey: bytes = field(repr=False)
    descriptor_path: Path | None = None
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    publish_descriptor: bool = False
    authentication_timeout: float = AUTHENTICATION_TIMEOUT

    def __post_init__(self) -> None:
        if len(self.authkey) < 32:
            raise ValueError("broker authkey must contain at least 32 random bytes")
        if self.authentication_timeout <= 0:
            raise ValueError("broker authentication timeout must be positive")

    def listen(
        self,
        client_handler: Callable[[socket.socket], None],
        error_handler: Callable[[Exception], None] | None = None,
    ) -> IPCServer:
        close_handler = None
        if self.publish_descriptor:
            close_handler = self._remove_descriptor

        try:
            server = AuthenticatedIPCServer(
                self.address,
                authkey=self.authkey,
                authentication_timeout=self.authentication_timeout,
                client_handler=client_handler,
                error_handler=error_handler,
                close_handler=close_handler,
            )
        except OSError as error:
            raise IPCError("could not create the broker IPC listener") from error

        try:
            if self.publish_descriptor:
                self._publish_descriptor(server.address)
        except BaseException:
            server.close()
            raise
        return server

    def connect(self, *, timeout: float = 2.0) -> socket.socket:
        try:
            client_socket = socket.create_connection(
                self.address.as_tuple(), timeout=max(0.0, timeout)
            )
        except OSError as error:
            raise BrokerUnavailableError(
                "foreground broker is not running; start `ezhpcy broker`"
            ) from error

        try:
            authenticate_client(client_socket, self.authkey, timeout)
            client_socket.settimeout(None)
        except (AuthenticationError, OSError, TimeoutError) as error:
            client_socket.close()
            raise IPCAuthenticationError(
                "broker authentication failed; restart the foreground broker"
            ) from error
        return client_socket

    def _publish_descriptor(self, address: IPCAddress) -> None:
        if self.descriptor_path is None:
            raise IPCError("broker runtime descriptor path is not configured")
        publish_runtime_descriptor(
            self.descriptor_path,
            address=address,
            authkey=self.authkey,
            instance_id=self.instance_id,
        )

    def _remove_descriptor(self) -> None:
        if self.descriptor_path is not None:
            remove_runtime_descriptor(
                self.descriptor_path,
                instance_id=self.instance_id,
            )


def default_descriptor_path(profile_name: str | None = None) -> Path:
    selected = profile_name or config.default_profile
    filename = f"broker-{selected}.json" if selected is not None else "broker.json"
    return config.local_file.runtime_dir / filename


def create_broker_backend(
    *,
    address: IPCAddress = _DEFAULT_BIND_ADDRESS,
    descriptor_path: Path | None = None,
    authkey: bytes | None = None,
    profile_name: str | None = None,
) -> AuthenticatedIPCBackend:
    """Create the server backend and its per-run authentication capability."""
    descriptor = descriptor_path or default_descriptor_path(profile_name)
    return AuthenticatedIPCBackend(
        address=address,
        authkey=authkey or secrets.token_bytes(32),
        descriptor_path=descriptor,
        publish_descriptor=True,
    )


def load_broker_backend(
    descriptor_path: Path | None = None,
    *,
    profile_name: str | None = None,
) -> AuthenticatedIPCBackend:
    """Load the broker endpoint and capability without exposing either in argv."""
    path = descriptor_path or default_descriptor_path(profile_name)
    descriptor = load_runtime_descriptor(path)
    try:
        return AuthenticatedIPCBackend(
            address=descriptor.address,
            authkey=descriptor.authkey,
            descriptor_path=path,
            instance_id=descriptor.instance_id,
        )
    except ValueError as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error
