"""
A job published under an alias: `ssh <alias>` reaches the job's sshd.

`publish` forwards a loopback port to the worker's sshd, through the login
connection, and writes the Host block that points OpenSSH there (see
`ezhpcy.tunnel.ssh_config`). It is an attachment of the job itself, not of its
node's connection: the block is withdrawn before the port closes, so that a
Host block never outlives the port it points at, when `Published.withdraw` is
called or the job is left.
"""

lazy import asyncio
lazy from dataclasses import replace

from ezhpcy.logging import get_logger
lazy from ezhpcy.attachments import Attachment
lazy from ezhpcy.cleanup import finish
lazy from ezhpcy.job.lifecycle import JobLifecycle
lazy from ezhpcy.job.status import PublishedStatus
lazy from ezhpcy.tunnel.lock import AliasReservation
lazy from ezhpcy.tunnel.ssh_config import (
    AliasProblem,
    WorkerHost,
    publish_host,
    withdraw_host,
)

logger = get_logger(__name__)


class Published:
    """
    A job published under `alias` (`Job.publish`): its Host block points
    OpenSSH at the loopback `port`. `problem` says why `ssh <alias>` won't
    reach it after all, if OpenSSH resolves the alias elsewhere since it was
    reserved; it is logged as a warning too.
    """

    def __init__(
        self,
        alias: str,
        port: int,
        problem: AliasProblem | None,
        attachment: Attachment,
    ) -> None:
        self.alias = alias
        self.port = port
        self.problem = problem
        self._attachment = attachment

    async def withdraw(self) -> None:
        """Remove the Host block, and close the port; once only."""
        await self._attachment.close()


async def publish(job: JobLifecycle, reservation: AliasReservation) -> Published:
    """
    Publish the entered `job` under the reserved alias. Raises `ValueError` if
    the reservation isn't held, a job is published under it already, or the
    login user can't be named in a Host block.
    """
    if not reservation.held:
        raise ValueError(f"the reservation of {reservation.alias!r} has ended")
    if reservation.published:
        raise ValueError(f"a job is published under {reservation.alias!r} already")
    worker = job.worker
    assert worker is not None
    worker_host = WorkerHost(reservation.alias, job.credentials)
    endpoint = str(worker.sshd)
    attachment = job.attach()
    reservation.published = True

    async def free_reservation() -> None:
        reservation.published = False

    # Pushed first, so that the reservation is free again only once the block
    # is withdrawn.
    attachment.push("free the alias's reservation", free_reservation)

    def on_connections(count: int) -> None:
        if isinstance(status := attachment.status, PublishedStatus):
            attachment.report(replace(status, connections=count))

    forwarded = await job.host.forward_local_port(
        worker.sshd, on_connections=on_connections
    )

    async def close_forwarded_port() -> None:
        # New connections are refused at once. Open ones end when the login
        # connection closes; waiting for them could take forever (FINDINGS.md,
        # "Phase 1").
        forwarded.close()

    attachment.push("close the forwarded port", close_forwarded_port)
    port = forwarded.port
    logger.debug(t"Forwarding port: port={port:detail} destination={endpoint:endpoint}")
    attachment.report(PublishedStatus(alias=reservation.alias, port=port))
    # Pushed before the block is written, and withdrawn before the port closes,
    # so that a Host block never outlives the port it points at.
    attachment.push(
        "remove the SSH configuration",
        lambda: asyncio.to_thread(withdraw_host, worker_host),
    )
    problem = await finish(
        "publish the SSH configuration",
        asyncio.to_thread(publish_host, worker_host, port),
    )
    return Published(reservation.alias, port, problem, attachment)
