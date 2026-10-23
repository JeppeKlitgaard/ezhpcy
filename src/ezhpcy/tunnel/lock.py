"""
One running tunnel per SSH host alias.

A tunnel holds an exclusive OS lock (`filelock.FileLock`: ``LockFileEx`` on
Windows, ``flock`` on POSIX) on ``<runtime_dir>/tunnels/<alias>.lock`` for as
long as it runs. The OS releases the lock when the process ends, however it
ends, so a free lock means no tunnel serves the alias, and a Host block left for
it is stale. Each lock object opens its own handle, so another one sees the lock
as held, even in the same process.

`reserve_alias` checks that `ssh <alias>` would reach the tunnel, then holds the
alias's lock; a job publishes its Host block under the reservation.
"""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
lazy import asyncio
lazy from collections.abc import AsyncGenerator, Generator
lazy from contextlib import ExitStack
lazy from pathlib import Path

lazy from filelock import FileLock, Timeout

from ezhpcy.messages import MessageError
lazy from ezhpcy.cleanup import cleanup
lazy from ezhpcy.config import config, validate_alias
lazy from ezhpcy.permissions import private_directory
lazy from ezhpcy.tunnel.ssh_config import AliasProblem, probe_alias
lazy from ezhpcy.types import ConfigError

LOCK_DIRECTORY_NAME = "tunnels"
# Releasing a lock is a local unlock; the OS releases it at exit anyway.
_LOCK_RELEASE_SECONDS = 1.0


class AliasInUse(MessageError, RuntimeError):
    """Another tunnel, in this process or another, holds the alias."""


class AliasUnreachable(MessageError, RuntimeError):
    """
    `ssh <alias>` would not reach a tunnel for the alias: `problem` says why, and
    its `include_lines`, if any, are the fix.
    """

    def __init__(self, alias: str, problem: AliasProblem) -> None:
        super().__init__(
            t"{f'ssh {alias}':suggestion} would not reach it: {problem.explanation}"
        )
        self.alias = alias
        self.problem = problem


def lock_path(alias: str) -> Path:
    return (
        config.local_file.runtime_dir
        / LOCK_DIRECTORY_NAME
        / f"{validate_alias(alias)}.lock"
    )


def _file_lock(path: Path) -> FileLock:
    """A lock that `acquire` takes at once or raises `Timeout`."""
    return FileLock(
        path,
        mode=0o600,
        blocking=False,
        # A soft lock (the file's existence) would outlive a killed tunnel, so
        # fail where `flock` isn't supported instead.
        fallback_to_soft=False,
        # Windows would remove the file on release; POSIX never does.
        preserve_lock_file=True,
    )


@contextmanager
def alias_lock(alias: str) -> Generator[None]:
    """
    Hold `alias`'s lock for the block. Raises `AliasInUse` at once if another
    tunnel holds it.
    """
    path = lock_path(alias)
    private_directory(path.parent)
    lock = _file_lock(path)
    try:
        lock.acquire()
    except Timeout:
        raise AliasInUse(t"a tunnel is already running for {alias:name}") from None
    try:
        yield
    finally:
        lock.release()


def alias_locked(alias: str) -> bool:
    """Whether a running tunnel holds `alias`'s lock."""
    path = lock_path(alias)
    # A tunnel creates the file before it takes the lock.
    if not path.exists():
        return False
    lock = _file_lock(path)
    try:
        lock.acquire()
    except Timeout:
        return True
    lock.release()
    return False


@asynccontextmanager
async def holding_lock(name: str) -> AsyncGenerator[None]:
    """
    Hold the alias lock `name` until left; releasing it is a cleanup step (see
    `ezhpcy.cleanup.cleanup`).
    """
    lock = ExitStack()
    lock.enter_context(alias_lock(name))
    try:
        yield
    finally:
        await cleanup(
            "release the alias lock",
            lambda: asyncio.to_thread(lock.close),
            seconds=_LOCK_RELEASE_SECONDS,
        )


@dataclass(eq=False)
class AliasReservation:
    """
    An OpenSSH Host alias, held for this process's tunnel (`reserve_alias`). A
    job is published under it once at a time: the alias has one Host block.
    """

    alias: str
    # Whether its `reserve_alias` block still runs.
    held: bool = True
    # Whether a job is published under it.
    published: bool = False


@asynccontextmanager
async def reserve_alias(alias: str) -> AsyncGenerator[AliasReservation]:
    """
    Hold `alias` for a tunnel, so that a job can be published under it.

    Before taking it, checks that `ssh <alias>` will reach the tunnel (see
    `ezhpcy.tunnel.ssh_config.probe_alias`): that `~/.ssh/config` includes
    EzHPCy's Host blocks, before any block that would shadow them. So a tunnel
    that can't serve fails before connecting, or waiting for a job. Released
    when the block is left.

    Raises `ConfigError` for a name that can't be an alias, `AliasUnreachable`
    (its `problem.include_lines` are the fix, if any), or `AliasInUse`, when
    another tunnel, in this process or another, holds it.
    """
    try:
        validate_alias(alias)
    except ValueError as error:
        raise ConfigError(t"{error}", value=alias) from error
    if (problem := await asyncio.to_thread(probe_alias, alias)) is not None:
        raise AliasUnreachable(alias, problem)
    reservation = AliasReservation(alias)
    async with holding_lock(alias):
        try:
            yield reservation
        finally:
            reservation.held = False
