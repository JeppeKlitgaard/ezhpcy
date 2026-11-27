"""
Downloading a directory tree from a host over SFTP into a copy of it here, which
replaces the old copy only once it is complete.
"""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
lazy import asyncio
lazy import os
lazy import shutil
lazy import stat
lazy import sys
lazy import tempfile
lazy from collections.abc import Sequence

lazy import asyncssh

from ezhpcy.logging import get_logger

logger = get_logger(__name__)

_FETCHES_AT_ONCE = 16


@dataclass(frozen=True, slots=True)
class _File:
    """A file on the host, at `relative` under the tree being downloaded."""

    relative: PurePosixPath
    remote: PurePosixPath
    # Where its links lead, so that a file reached by several paths is fetched once.
    resolved: PurePosixPath
    size: int | None
    # In whole seconds, if the host says.
    mtime: int | None
    executable: bool


@dataclass(slots=True)
class _Tree:
    directories: list[PurePosixPath]
    files: list[_File]


async def download(
    sftp: asyncssh.SFTPClient, remote: PurePosixPath, local: Path
) -> None:
    """Make `local` a copy of the directory `remote` (see `Host.download`)."""
    tree = await _walk(sftp, remote)
    local.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{local.name}.download-"
    _remove_leftovers(local.parent, prefix)
    building = Path(tempfile.mkdtemp(prefix=prefix, dir=local.parent))
    try:
        for directory in tree.directories:
            (building / directory).mkdir()
        fetched = await _fill(sftp, tree.files, local, building)
        _swap(building, local)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    logger.info(
        t"Downloaded {remote:path} to {local:path}, fetching {fetched:detail} of "
        t"its {len(tree.files):detail} files."
    )


async def _walk(sftp: asyncssh.SFTPClient, remote: PurePosixPath) -> _Tree:
    """
    The directories and files under `remote`, in sorted order, following links.
    A link to a directory that holds it is skipped, so that it can't loop.
    """
    tree = _Tree([], [])

    async def walk(
        directory: PurePosixPath,
        relative: PurePosixPath,
        resolved: PurePosixPath,
        ancestors: frozenset[PurePosixPath],
    ) -> None:
        entries = await sftp.readdir(directory)
        for entry in sorted(entries, key=lambda entry: str(entry.filename)):
            name = str(entry.filename)
            if name in (".", ".."):
                continue
            path = directory / name
            attrs = entry.attrs
            linked = stat.S_ISLNK(attrs.permissions or 0)
            if linked:
                try:
                    attrs = await sftp.stat(path)
                except asyncssh.SFTPError as error:
                    logger.debug(t"Skipped a broken link: link={path:path} {error!r}")
                    continue
            mode = attrs.permissions or 0
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                logger.debug(t"Skipped what isn't a file or directory: {path:path}")
                continue
            # Only a link leads anywhere but under its directory's resolved path.
            target = (
                PurePosixPath(await sftp.realpath(path)) if linked else resolved / name
            )
            if stat.S_ISREG(mode):
                tree.files.append(
                    _File(
                        relative / name,
                        path,
                        target,
                        attrs.size,
                        attrs.mtime,
                        bool(mode & stat.S_IXUSR),
                    )
                )
            elif target in ancestors:
                logger.debug(
                    t"Skipped a link to a directory above it: link={path:path}"
                )
            else:
                tree.directories.append(relative / name)
                await walk(path, relative / name, target, ancestors | {target})

    resolved = PurePosixPath(await sftp.realpath(remote))
    await walk(remote, PurePosixPath(), resolved, frozenset({resolved}))
    return tree


async def _fill(
    sftp: asyncssh.SFTPClient, files: Sequence[_File], local: Path, building: Path
) -> int:
    """
    Put `files` in `building`: a file unchanged since `local`'s copy linked from
    there, and the others fetched, once however many paths lead to them. Returns
    how many were fetched.
    """
    paths: dict[PurePosixPath, list[_File]] = {}
    for file in files:
        paths.setdefault(file.resolved, []).append(file)
    fetches = asyncio.Semaphore(_FETCHES_AT_ONCE)
    fetched = 0

    async def place(same: Sequence[_File]) -> None:
        nonlocal fetched
        first = building / same[0].relative
        old = next(
            (
                local / file.relative
                for file in same
                if _unchanged(local / file.relative, file)
            ),
            None,
        )
        if old is not None:
            _link(old, first)
        else:
            async with fetches:
                await sftp.get(same[0].remote, first, follow_symlinks=True)
            fetched += 1
            _set_attributes(first, same[0])
        for file in same[1:]:
            _link(first, building / file.relative)

    # A group, so that when one fails the others have stopped before `building`
    # is removed.
    try:
        async with asyncio.TaskGroup() as group:
            for same in paths.values():
                group.create_task(place(same))
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None
    return fetched


def _unchanged(path: Path, file: _File) -> bool:
    """
    Whether `path` is `file` as fetched before: of its size and modification
    time, and its executable bit where this platform has the bits.
    """
    try:
        status = path.stat()
    except OSError:
        return False
    return (
        file.size is not None
        and file.mtime is not None
        and stat.S_ISREG(status.st_mode)
        and status.st_size == file.size
        and int(status.st_mtime) == file.mtime
        and (
            sys.platform == "win32"
            or bool(status.st_mode & stat.S_IXUSR) == file.executable
        )
    )


def _set_attributes(path: Path, file: _File) -> None:
    """
    Give the fetched `path` `file`'s modification time, and the mode of a file
    the user can read and write, executable if `file` is.
    """
    if sys.platform != "win32":
        path.chmod(0o755 if file.executable else 0o644)
    if file.mtime is not None:
        os.utime(path, (file.mtime, file.mtime))


def _link(source: Path, target: Path) -> None:
    """Hard-link `source` at `target`, or copy it where links can't be made."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _swap(building: Path, local: Path) -> None:
    """Put `building` in `local`'s place, leaving `local` as it was if that fails."""
    aside = building.with_name(f"{building.name}.old")
    try:
        local.rename(aside)
    except FileNotFoundError:
        building.rename(local)
        return
    try:
        building.rename(local)
    except BaseException:
        aside.rename(local)
        raise
    try:
        shutil.rmtree(aside)
    except OSError as error:
        # The next download removes it.
        logger.debug(t"Could not remove the old copy {aside:path}: {error!r}")


def _remove_leftovers(directory: Path, prefix: str) -> None:
    """Remove what downloads that were killed left in `directory`."""
    for path in directory.iterdir():
        if path.name.startswith(prefix):
            logger.debug(t"Removing a killed download's {path:path}.")
            shutil.rmtree(path)
