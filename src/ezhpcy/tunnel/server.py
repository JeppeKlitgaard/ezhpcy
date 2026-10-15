"""The tunnel server: relays each authenticated proxy to the worker."""

import logging
lazy import asyncio
lazy import itertools
lazy import secrets
lazy import time
lazy from types import TracebackType
lazy from typing import Self

lazy import asyncssh

from ezhpcy.ipc.protocol import (
    AUTHENTICATION_TIMEOUT,
    READY,
    authenticate_server,
    rejection,
)
lazy from ezhpcy.ipc import LOOPBACK_HOST, IPCError
lazy from ezhpcy.ssh import RemoteHost

logger = logging.getLogger(__name__)

TRANSPORT_LOST = "login-node transport was lost; restart the tunnel"
_CHUNK_BYTES = 64 * 1024


class TunnelServer:
    """
    Listen on a loopback port while inside `async with`.

    Each proxy that proves it holds `authkey` gets its own `direct-tcpip` channel
    to `destination` over the login connection, relayed until both sides have
    sent end of file. A proxy that can't get a channel is told why, and the
    server carries on.
    """

    def __init__(
        self,
        host: RemoteHost,
        destination: tuple[str, int],
        *,
        authentication_timeout: float = AUTHENTICATION_TIMEOUT,
    ) -> None:
        self.host = host
        self.destination = destination
        self.authkey = secrets.token_bytes(32)
        self._authentication_timeout = authentication_timeout
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task[None]] = set()
        self._connection_ids = itertools.count(1)

    @property
    def port(self) -> int:
        assert self._server is not None, "the tunnel server is not listening"
        return self._server.sockets[0].getsockname()[1]

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve_client, LOOPBACK_HOST, 0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """
        Stop listening and end every proxy's relay, without waiting on any proxy:
        data still queued for one that has stopped reading is dropped.
        """
        if self._server is None:
            return
        self._server.close()
        clients = list(self._clients)
        for client in clients:
            client.cancel()
        # The handlers clean up without awaiting anything, so this is quick.
        await asyncio.gather(*clients, return_exceptions=True)
        # Closing a connection first sends what is queued for it, and
        # `wait_closed` waits for every connection to close.
        self._server.abort_clients()
        await self._server.wait_closed()

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._server is None or not self._server.is_serving():
            # Accepted just before `close`, which therefore can't cancel it.
            writer.close()
            return
        task = asyncio.current_task()
        assert task is not None
        self._clients.add(task)
        connection_id = next(self._connection_ids)
        started = time.monotonic()
        logger.debug(
            "Tunnel client accepted: connection=%d destination=%s:%d",
            connection_id,
            *self.destination,
        )
        worker_writer = None
        try:
            if not await self._authenticate(reader, writer):
                return
            if self.host.closed.done():
                writer.write(rejection(TRANSPORT_LOST))
                await writer.drain()
                return
            try:
                worker_reader, worker_writer = await self.host.open_tcp(
                    *self.destination
                )
            except (asyncssh.Error, OSError) as error:
                writer.write(rejection(f"worker channel could not be opened: {error}"))
                await writer.drain()
                return
            logger.debug("Worker channel opened: connection=%d", connection_id)
            writer.write(bytes((READY,)))
            await self._relay(
                connection_id, reader, writer, worker_reader, worker_writer
            )
        except OSError as error:
            # The proxy went away while being told whether it may connect.
            logger.error("Tunnel client error: %s", error)
        finally:
            if worker_writer is not None:
                worker_writer.close()
            writer.close()
            self._clients.discard(task)
            logger.debug(
                "Tunnel client closed: connection=%d duration_seconds=%.3f",
                connection_id,
                time.monotonic() - started,
            )

    async def _authenticate(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        try:
            async with asyncio.timeout(self._authentication_timeout):
                await authenticate_server(reader, writer, self.authkey)
        # Before `OSError`, of which it is a subclass.
        except TimeoutError:
            logger.error("Tunnel client error: local client authentication timed out")
            return False
        except IPCError, EOFError, OSError:
            logger.error(
                "Tunnel client error: rejected a local client during tunnel "
                "authentication"
            )
            return False
        return True

    async def _relay(
        self,
        connection_id: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        worker_reader: asyncssh.SSHReader[bytes],
        worker_writer: asyncssh.SSHWriter[bytes],
    ) -> None:
        started = time.monotonic()
        try:
            async with asyncio.TaskGroup() as group:
                to_worker = group.create_task(_copy(reader, worker_writer))
                to_proxy = group.create_task(_copy(worker_reader, writer))
        except* (asyncssh.Error, OSError) as failure:
            # An unexpected mid-session drop
            logger.warning(
                "Worker channel relay failed: connection=%d duration_seconds=%.3f "
                "error=%r",
                connection_id,
                time.monotonic() - started,
                failure.exceptions[0],
            )
        else:
            logger.debug(
                "Worker channel reached EOF: connection=%d duration_seconds=%.3f "
                "bytes_to_worker=%d bytes_to_proxy=%d",
                connection_id,
                time.monotonic() - started,
                to_worker.result(),
                to_proxy.result(),
            )


async def _copy(
    reader: asyncio.StreamReader | asyncssh.SSHReader[bytes],
    writer: asyncio.StreamWriter | asyncssh.SSHWriter[bytes],
) -> int:
    """Copy until end of file, then pass the end of file on; return the bytes copied."""
    copied = 0
    while data := await reader.read(_CHUNK_BYTES):
        writer.write(data)
        await writer.drain()
        copied += len(data)
    writer.write_eof()
    return copied
