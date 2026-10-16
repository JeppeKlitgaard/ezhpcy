"""
One running tunnel per SSH host alias.

A tunnel holds an exclusive OS lock (`filelock.FileLock`: ``LockFileEx`` on
Windows, ``flock`` on POSIX) on ``<runtime_dir>/tunnels/<alias>.lock`` for as
long as it runs. The OS releases the lock when the process ends, however it
ends, so a free lock means no tunnel serves the alias, and a Host block left for
it is stale. Each lock object opens its own handle, so another one sees the lock
as held, even in the same process.
"""

from contextlib import contextmanager
lazy import os
lazy from collections.abc import Generator
lazy from pathlib import Path

lazy from filelock import FileLock, Timeout

lazy from ezhpcy.config import config, validate_alias
lazy from ezhpcy.tunnel import TunnelError

LOCK_DIRECTORY_NAME = "tunnels"


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
    Hold `alias`'s lock for the block. Raises `TunnelError` at once if another
    tunnel holds it.
    """
    path = lock_path(alias)
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    lock = _file_lock(path)
    try:
        lock.acquire()
    except Timeout:
        raise TunnelError(f"a tunnel is already running for `{alias}`") from None
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
