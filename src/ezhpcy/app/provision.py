"""
An app's environment and assets on the host it runs on, under the apps root
(by default `<XDG cache>/ezhpcy-apps` there, see `RemoteState.apps_root`):

    <apps root>/v<layout>/     by `APPS_LAYOUT_VERSION`
      pixi-<version>/envs/<env>/   by `PIXI_VERSION`: the Pixi files, and Pixi's
                               `.pixi/`
      pixi-<version>/envs/<env>/.ezhpcy-installed   once `pixi install --frozen`
                               succeeded, saying which EzHPCy and Pixi installed it
      cache/                   Pixi's cache for app environments, of every version
      .install.lock/           held while an environment installs
      assets/<assets>/         the asset files, read-only, once an attempt completed
      assets/<assets>.<attempt>/  one attempt, while it runs
      assets/<assets>.lock/    held while an attempt runs

So installs of other layouts, and environments of other Pixi versions, are
never used, and stay beside these for the EzHPCy versions that use them: a
shared root serves clients of several versions at once. Pixi's cache is made
to be shared by its versions, so they share one, and their environments are
linked to the same files. An environment's
directory is named by its workspace's name and version, its Pixi environment
and its key (`environment_key`), such as `notebooks@1.0.0__default__<key>`, so
that the apps of one workspace share it; assets' by their app's normalised id
and version and their key (`assets_key`), such as `lab@v1.0__<key>`.

**Shared roots** (`site.shared_app_roots`) are apps roots that administrators
install apps into, for their users to run them from. They are looked in first,
in order, for an installed environment and for published assets; what none has
is installed in the user's own apps root. A run never writes in a shared root.
Each has its own Pixi cache, as Pixi links an environment's files to its cache
only on one filesystem, and only to files that their user owns or may write.

Everything EzHPCy creates in an apps root takes its mode from the root's
(`_SHARED_MODES`): in the user's own, its owner's alone (as `private_directory`);
in a shared root, which must exist, as the root's own, of a few that say who
writes (its owner, or its group too) and who reads (everyone, or its group), and
whose setgid bit its directories keep, so that what they hold keeps its group. Pixi gives what it installs its packages' own modes, whatever
the umask, often group-writable: so an environment, like an asset attempt, is
open only to those who may write in the root while it is built, and then, in a
shared root, takes the root's modes all at once (`chmod -R`; in the user's own,
the root's mode keeps others out). Pixi's cache stays theirs alone. Asset files
are read-only, to their owner too.

**Environments** are installed by EzHPCy's own Pixi, in place: Pixi detects
and repairs an install that was interrupted, and an environment keyed by its Pixi files never goes out
of date. An install holds the apps root's one lock, as file locks don't hold
across a cluster's nodes, so that Pixi's shared cache is never written by two
installs at once; on success it writes `.ezhpcy-installed`. A run uses only an
environment with that marker, and never installs (`pixi run --as-is`).

**Assets** are built once by `publish_once`: each attempt builds in a fresh
directory of its own, and only once it has succeeded is it renamed to its key,
in one atomic step. SFTP's rename never replaces what has the name already, so
of two attempts that finish at once, the second is removed, and the first's
used. An attempt that failed, typically on a full disk or quota, is removed, and
never used or built over: the next one removes any leftovers, while holding the
key's lock, and starts afresh.

The locks are directories, as `mkdir` is atomic, on NFS and BeeGFS too.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
lazy import asyncio
lazy import json
lazy import secrets
lazy import stat
lazy import time
lazy from collections.abc import AsyncGenerator

lazy import asyncssh

from ezhpcy.logging import get_logger
lazy from ezhpcy.app.manifest import (
    App,
    AssetFile,
    Environment,
    assets_key,
    environment_key,
)
lazy from ezhpcy.constants import EZHPCY_VERSION
lazy from ezhpcy.provision_host import ProvisioningError, provision_pixi
lazy from ezhpcy.ssh import CommandFailed, RemoteHost, open_file
lazy from ezhpcy.types import RemoteState
lazy from ezhpcy.versions import APPS_LAYOUT_VERSION, PIXI_VERSION

logger = get_logger(__name__)

ENVIRONMENTS_DIRECTORY = "envs"
ASSETS_DIRECTORY = "assets"
PIXI_CACHE_DIRECTORY = "cache"
# In an environment's directory, once its install succeeded.
INSTALLED_NAME = ".ezhpcy-installed"
# Touched in an entry's directory by each run that uses it, for pruning by age.
LAST_USED_NAME = ".ezhpcy-last-used"
_LOCK_SUFFIX = ".lock"
# The one lock of a layout's installs, as they share Pixi's cache.
_INSTALL_LOCK_NAME = f".install{_LOCK_SUFFIX}"
# The names of a layout's directory, and of a Pixi version's in it.
_LAYOUT_NAME = re.compile(r"^v[0-9]+$")
_PIXI_NAME = re.compile(r"^pixi-[0-9A-Za-z][0-9A-Za-z.+!_-]*$")

# How often a provisioner renews the lock it holds, and how long a lock may go
# unrenewed before others take its holder for dead. Renewed by its directory's
# modification time, which others compare only with itself, so that clocks
# needn't agree.
_LOCK_RENEW_SECONDS = 20.0
_LOCK_STALE_SECONDS = 120.0
# How often a provisioner waiting for a lock looks again.
_LOCK_POLL_SECONDS = 2.0
# How long removing a failed attempt may take: it may hold gigabytes.
_REMOVE_SECONDS = 120.0
# How many asset files are uploaded at once.
_UPLOADS_AT_ONCE = 16


@dataclass(frozen=True, slots=True)
class AppsLayout:
    """
    Where an apps root keeps what, and the shared roots looked in first (see the
    module's docstring).
    """

    root: PurePosixPath
    shared: tuple[PurePosixPath, ...] = ()

    @property
    def layout(self) -> PurePosixPath:
        return self.root / f"v{APPS_LAYOUT_VERSION}"

    @property
    def pixi(self) -> PurePosixPath:
        """What EzHPCy's Pixi makes."""
        return self.layout / f"pixi-{PIXI_VERSION}"

    @property
    def environments(self) -> PurePosixPath:
        return self.pixi / ENVIRONMENTS_DIRECTORY

    @property
    def assets(self) -> PurePosixPath:
        return self.layout / ASSETS_DIRECTORY

    @property
    def pixi_cache(self) -> PurePosixPath:
        return self.layout / PIXI_CACHE_DIRECTORY

    def environment(self, environment: Environment) -> PurePosixPath:
        """The directory of `environment` (see the module's docstring)."""
        name, version = environment.workspace
        key = environment_key(environment)
        return self.environments / f"{name}@{version}__{environment.environment}__{key}"


@dataclass(frozen=True, slots=True)
class _Modes:
    """The modes of what provisioning creates in an apps root, by the root's."""

    directory: int
    file: int
    # The assets', and their directories' and executables' (`read_only`).
    read_only_file: int
    read_only_executable: int
    # What is being built, and Pixi's cache: only those who may write in the root
    # may enter.
    building: int
    # In a shared root, the `chmod -R` mode that gives what Pixi installed the
    # root's, as Pixi gives it its packages' own; in a user's own, nothing, as
    # its root keeps others out.
    installed: str | None = None

    def read_only(self, *, executable: bool) -> int:
        return self.read_only_executable if executable else self.read_only_file


# A user's own apps root's: its owner's alone, as `private_directory`.
_PRIVATE = _Modes(0o700, 0o600, 0o400, 0o500, 0o700)
# A shared root's, by its permission bits.
_SHARED_MODES = {
    # Its owner writes, and everyone reads.
    0o755: _Modes(0o755, 0o644, 0o444, 0o555, 0o700, "a+rX,go-w"),
    # Its group writes too, and everyone reads.
    0o775: _Modes(0o775, 0o664, 0o444, 0o555, 0o770, "a+rX,g+w,o-w"),
    # Its owner writes, and its group reads.
    0o750: _Modes(0o750, 0o640, 0o440, 0o550, 0o700, "g+rX,g-w,o-rwx"),
    # Its group writes and reads.
    0o770: _Modes(0o770, 0o660, 0o440, 0o550, 0o770, "g+rwX,o-rwx"),
}


@dataclass(frozen=True, slots=True)
class ProvisionedApp:
    """
    Where an app's installed environment and published assets are: the Pixi
    manifest to run it by, and the assets' directory.
    """

    pixi_manifest: PurePosixPath
    assets: PurePosixPath | None

    @property
    def environment(self) -> PurePosixPath:
        """The environment's directory."""
        return self.pixi_manifest.parent


# Builds an attempt in the directory it is given; raises if it can't.
type Build = Callable[[PurePosixPath], Awaitable[None]]


async def stale_installs(
    sftp: asyncssh.SFTPClient, layout: AppsLayout
) -> list[PurePosixPath]:
    """
    What this EzHPCy never uses in the apps root, as another EzHPCy or Pixi
    version made it: other layouts' installs, and in this one, other Pixi
    versions' environments. For a user's own apps root alone: a shared root
    keeps them, for the clients of those versions.
    """
    stale: list[PurePosixPath] = []
    for directory, current, pattern in (
        (layout.root, layout.layout, _LAYOUT_NAME),
        (layout.layout, layout.pixi, _PIXI_NAME),
    ):
        try:
            names = await sftp.listdir(directory)
        except asyncssh.SFTPNoSuchFile:
            continue
        stale += [
            directory / name
            for name in sorted(names)
            if name != current.name and pattern.fullmatch(name)
        ]
    return stale


# Where an app's environment or assets are in an apps root, if they are.
type _Find = Callable[[AppsLayout], Awaitable[PurePosixPath | None]]


async def provision_app(
    host: RemoteHost,
    remote_state: RemoteState,
    layout: AppsLayout,
    app: App,
    *,
    shared: bool = False,
) -> ProvisionedApp:
    """
    Install EzHPCy's Pixi on `host` if it is missing, then the app's
    environment, and upload its assets, each unless a shared root or the apps
    root has it already, and note that a run uses those in the apps root. With
    `shared`, the apps root is a shared one, for others to run apps from: it
    must exist, and others mustn't write there. Raises `ProvisioningError`.
    """
    await provision_pixi(host, remote_state)
    async with host.sftp() as sftp:
        if shared:
            modes = await _shared_modes(sftp, layout.root)
        else:
            modes = _PRIVATE
            await _directory(sftp, layout.root, modes.directory)
        shared_layouts = [AppsLayout(root) for root in layout.shared]
        used: list[PurePosixPath] = []
        what = f"environment of app {app}"
        found = await _first(
            shared_layouts, _environment(sftp, app.environment), what=what
        )
        if found is not None:
            directory = found[1]
            logger.debug(t"Using the shared {what}: directory={directory:path}")
        else:
            directory = await _install(host, sftp, remote_state, layout, app, modes)
            used.append(directory)
        assets = None
        if app.assets is not None:
            key = _assets_name(app, app.assets.files)
            what = f"assets of app {app}"
            if found := await _first(shared_layouts, _assets(sftp, key), what=what):
                assets = found[1]
                logger.debug(t"Using the shared {what}: directory={assets:path}")
            else:
                assets = await publish_once(
                    host,
                    sftp,
                    layout.assets,
                    key,
                    _assets_builder(sftp, app.assets.files, modes),
                    what=what,
                    modes=modes,
                )
                used.append(assets)
        await _mark_used(sftp, used)
    return ProvisionedApp(directory / app.environment.pixi.name, assets)


async def validate_app(
    host: RemoteHost, remote_state: RemoteState, layout: AppsLayout, app: App
) -> ProvisionedApp:
    """
    Where the app's environment and assets are, in a shared root or the apps
    root, without installing anything, and note that a run uses those in the
    apps root; raises `ProvisioningError` if EzHPCy's Pixi, the environment or
    the assets aren't there for the app's current files.
    """
    layouts = [*(AppsLayout(root) for root in layout.shared), layout]
    missing: list[PurePosixPath] = []
    used: list[PurePosixPath] = []
    directory = assets = None
    async with host.sftp() as sftp:
        if not await sftp.exists(remote_state.pixi_executable()):
            missing.append(remote_state.pixi_executable())
        what = f"environment of app {app}"
        if found := await _first(
            layouts, _environment(sftp, app.environment), what=what
        ):
            found_in, directory = found
            if found_in is layout:
                used.append(directory)
        else:
            missing += [
                each.environment(app.environment) / INSTALLED_NAME for each in layouts
            ]
        if app.assets is not None:
            key = _assets_name(app, app.assets.files)
            what = f"assets of app {app}"
            if found := await _first(layouts, _assets(sftp, key), what=what):
                found_in, assets = found
                if found_in is layout:
                    used.append(assets)
            else:
                missing += [each.assets / key for each in layouts]
        if not missing:
            await _mark_used(sftp, used)
    if missing:
        raise ProvisioningError(
            t"app {str(app):name} isn't provisioned on this host for its current "
            t"files (missing: {missing:path})"
        )
    assert directory is not None
    return ProvisionedApp(directory / app.environment.pixi.name, assets)


def _assets_name(app: App, files: Sequence[AssetFile]) -> str:
    """The name of the app's assets in `assets/` (see the module's docstring)."""
    return f"{app.normalised_id}@{app.version}__{assets_key(files)}"


def _environment(sftp: asyncssh.SFTPClient, environment: Environment) -> _Find:
    """Finds `environment` where it is installed."""

    async def find(layout: AppsLayout) -> PurePosixPath | None:
        directory = layout.environment(environment)
        return directory if await sftp.exists(directory / INSTALLED_NAME) else None

    return find


def _assets(sftp: asyncssh.SFTPClient, key: str) -> _Find:
    """Finds the assets of `key` where they are published."""

    async def find(layout: AppsLayout) -> PurePosixPath | None:
        return await _published(sftp, layout.assets / key)

    return find


async def _first(
    layouts: Sequence[AppsLayout], find: _Find, *, what: str
) -> tuple[AppsLayout, PurePosixPath] | None:
    """
    The first of `layouts` where `find` finds the app's `what`, and where; one
    that can't be read is passed over.
    """
    for layout in layouts:
        try:
            if (found := await find(layout)) is not None:
                return layout, found
        except asyncssh.SFTPError as error:
            logger.debug(
                t"Could not look for the {what} in {layout.root:path}: {error!r}"
            )
            continue
        logger.debug(t"No {what} in {layout.root:path}.")
    return None


async def _shared_modes(sftp: asyncssh.SFTPClient, root: PurePosixPath) -> _Modes:
    """
    The modes of what is created in the shared root `root`, by its own: it must
    be a directory, of one of `_SHARED_MODES`, so that others never write there,
    as they would then change what its users run. Its setgid bit is kept.
    """
    try:
        attrs = await sftp.stat(root)
    except asyncssh.SFTPError as error:
        raise ProvisioningError(
            t"Could not use the shared root {root:path}: {error}. Create it first, "
            t"with the group and mode that what is installed there is to have."
        ) from error
    if attrs.type != asyncssh.FILEXFER_TYPE_DIRECTORY:
        raise ProvisioningError(t"The shared root {root:path} isn't a directory.")
    mode = attrs.permissions or 0
    if (modes := _SHARED_MODES.get(mode & 0o777)) is None:
        supported = [f"{each:o}" for each in _SHARED_MODES]
        raise ProvisioningError(
            t"The shared root {root:path} has mode {f'{mode & 0o7777:04o}':detail}; "
            t"give it one of {supported:choice}, with or without the setgid bit, "
            t"which say who writes and who reads, and never let others write there."
        )
    setgid = mode & stat.S_ISGID
    return replace(
        modes, directory=modes.directory | setgid, building=modes.building | setgid
    )


async def _directory(sftp: asyncssh.SFTPClient, path: PurePosixPath, mode: int) -> None:
    """
    Create a directory, and those of its parents that are missing, of `mode`,
    unless it exists. Each one's mode is set after creating it too, as the
    server's umask may take bits away.
    """
    missing = []
    while not await sftp.isdir(path):
        missing.append(path)
        if path.parent == path:
            break
        path = path.parent
    for directory in reversed(missing):
        try:
            await sftp.mkdir(directory, attrs=asyncssh.SFTPAttrs(permissions=mode))
        except asyncssh.SFTPFailure:
            # Made by another provisioner meanwhile.
            if not await sftp.isdir(directory):
                raise
        await sftp.chmod(directory, mode)


async def _file(
    sftp: asyncssh.SFTPClient, path: PurePosixPath, modes: _Modes, content: str = ""
) -> None:
    """Create a file in `modes`, holding `content`."""
    async with open_file(sftp, path, "w") as file:
        await file.write(content)
    await sftp.chmod(path, modes.file)


async def _mark_used(
    sftp: asyncssh.SFTPClient, directories: Sequence[PurePosixPath]
) -> None:
    """
    Note that a run uses the environment and assets in `directories` now, for
    pruning by age: by the time of their `LAST_USED_NAME` file, which their
    owner may set even where it can't write, as in the read-only assets.
    """
    now = int(time.time())
    for directory in directories:
        path = directory / LAST_USED_NAME
        try:
            await sftp.utime(path, (now, now))
        except asyncssh.SFTPNoSuchFile:
            try:
                async with open_file(sftp, path, "w"):
                    pass
            except asyncssh.SFTPError as error:
                logger.debug(t"Could not mark {directory:path} used: {error!r}")


async def _install(
    host: RemoteHost,
    sftp: asyncssh.SFTPClient,
    remote_state: RemoteState,
    layout: AppsLayout,
    app: App,
    modes: _Modes,
) -> PurePosixPath:
    """
    The directory of the app's environment in the apps root, installing it
    first unless it is installed (see the module's docstring).
    """
    environment = app.environment
    directory = layout.environment(environment)
    installed = directory / INSTALLED_NAME
    what = f"environment of app {app}"
    if await sftp.exists(installed):
        logger.debug(t"Using the installed {what}: directory={directory:path}")
        return directory
    await _directory(sftp, layout.environments, modes.directory)
    lock = layout.layout / _INSTALL_LOCK_NAME
    async with _held(sftp, lock, modes, what=what):
        # Another provisioner may have installed it while this one waited.
        if await sftp.exists(installed):
            logger.debug(t"Using the {what}, installed meanwhile.")
            return directory
        unshared = ", as no shared root has it" if layout.shared else ""
        logger.info(t"Installing the {what} in {directory:path}{unshared}.")
        started = time.monotonic()
        try:
            # Also an earlier attempt's, never opened, and a cache Pixi made.
            for building in (layout.pixi_cache, directory):
                await _directory(sftp, building, modes.building)
                await sftp.chmod(building, modes.building)
            await _warn_if_apart(host, layout)
            # The same files as any earlier attempt's, by the key.
            manifest = directory / environment.pixi.name
            await sftp.put(environment.pixi, manifest)
            await sftp.put(environment.lock, directory / environment.lock.name)
            await host.run_pixi(
                [
                    "install",
                    "--frozen",
                    "--environment",
                    environment.environment,
                    "--manifest-path",
                    str(manifest),
                ],
                remote_state,
                cache_dir=layout.pixi_cache,
            )
            if modes.installed is not None:
                await host.run(["chmod", "-R", modes.installed, "--", str(directory)])
            installer = {"ezhpcy": EZHPCY_VERSION, "pixi": PIXI_VERSION}
            await _file(sftp, installed, modes, json.dumps(installer) + "\n")
        except (CommandFailed, asyncssh.Error, OSError) as error:
            free = await _free_space(host, directory)
            raise ProvisioningError(
                t"Could not install the {what}: {_reason(error)}. {free} The next "
                t"attempt repairs what this one left."
            ) from error
        elapsed = time.monotonic() - started
        logger.info(t"Installed the {what} in {elapsed:detail:.0f} seconds.")
    return directory


async def _warn_if_apart(host: RemoteHost, layout: AppsLayout) -> None:
    """
    Warn when Pixi's cache is on another filesystem than the environments, as
    Pixi then copies every package into each, where it would link them.
    """
    paths = [str(layout.environments), str(layout.pixi_cache)]
    try:
        devices = (await host.run(["stat", "-c", "%d", "--", *paths])).split()
    except (CommandFailed, asyncssh.Error, OSError) as error:
        logger.debug(t"Could not tell the filesystems of {paths:path}: {error!r}")
        return
    if len(set(devices)) > 1:
        logger.warning(
            t"Pixi's cache {layout.pixi_cache:path} is on another filesystem than "
            t"{layout.environments:path}, so each environment holds a copy of "
            t"every package."
        )


async def publish_once(
    host: RemoteHost,
    sftp: asyncssh.SFTPClient,
    directory: PurePosixPath,
    key: str,
    build: Build,
    *,
    what: str,
    modes: _Modes,
) -> PurePosixPath:
    """
    `directory / key`, building it first with `build` unless it is published
    already (see the module's docstring), and creating what it does in `modes`.

    Raises `ProvisioningError` if the build fails, saying how much space is left,
    as a full disk or quota is the likely cause, after removing what it left.
    """
    entry = directory / key
    if (published := await _published(sftp, entry)) is not None:
        logger.debug(t"Using published {what}: entry={entry:path}")
        return published
    await _directory(sftp, directory, modes.directory)
    async with _held(sftp, directory / f"{key}{_LOCK_SUFFIX}", modes, what=what):
        # Another provisioner may have published it while this one waited.
        if (published := await _published(sftp, entry)) is not None:
            logger.debug(t"Using {what}, published meanwhile: entry={entry:path}")
            return published
        await _remove_leftovers(host, sftp, directory, key)
        attempt = directory / f"{key}.{secrets.token_hex(8)}"
        logger.info(t"Provisioning the {what} in {attempt:path}.")
        started = time.monotonic()
        try:
            await _directory(sftp, attempt, modes.building)
            # First, while the attempt's directory is writable (see `_mark_used`).
            await _file(sftp, attempt / LAST_USED_NAME, modes)
            await build(attempt)
            # Read-only already: moving a directory within its parent doesn't
            # write in it.
            await sftp.rename(attempt, entry)
        except BaseException as error:
            await _remove(host, [attempt])
            # A provisioner that broke a lock it took for dead may have won.
            if isinstance(error, asyncssh.SFTPError) and (
                published := await _published(sftp, entry)
            ):
                logger.debug(t"Using {what}, published by another provisioner.")
                return published
            if not isinstance(error, CommandFailed | asyncssh.Error | OSError):
                raise
            free = await _free_space(host, directory)
            raise ProvisioningError(
                t"Could not provision the {what}: {_reason(error)}. {free}"
            ) from error
        elapsed = time.monotonic() - started
        logger.info(t"Provisioned the {what} in {elapsed:detail:.0f} seconds.")
        return entry


async def _published(
    sftp: asyncssh.SFTPClient, entry: PurePosixPath
) -> PurePosixPath | None:
    """`entry`, if it is published: only a completed attempt has its name."""
    try:
        if (await sftp.lstat(entry)).type != asyncssh.FILEXFER_TYPE_DIRECTORY:
            return None
    except asyncssh.SFTPNoSuchFile:
        return None
    return entry


@asynccontextmanager
async def _held(
    sftp: asyncssh.SFTPClient, lock: PurePosixPath, modes: _Modes, *, what: str
) -> AsyncGenerator[None]:
    """
    Hold `lock`, a directory, as `mkdir` creates it atomically, and renew it
    while held. Waits while another provisioner holds it, unless its holder
    hasn't renewed it within `_LOCK_STALE_SECONDS`, and is taken for dead.
    """
    await _acquire(sftp, lock, modes, what=what)
    renewing = asyncio.create_task(_renew(sftp, lock))
    try:
        yield
    finally:
        renewing.cancel()
        await asyncio.gather(renewing, return_exceptions=True)
        try:
            await sftp.rmdir(lock)
        except asyncssh.SFTPError as error:
            logger.warning(t"Could not release the lock {lock:path}: {error}")


async def _acquire(
    sftp: asyncssh.SFTPClient, lock: PurePosixPath, modes: _Modes, *, what: str
) -> None:
    # The lock's last renewal seen, and when it was seen to change, by this clock.
    seen: tuple[int | None, float] | None = None
    while True:
        try:
            # Only its holder changes it, and the others remove it from the
            # directory it is in: so its mode as asked, less the umask's bits.
            await sftp.mkdir(
                lock, attrs=asyncssh.SFTPAttrs(permissions=modes.directory)
            )
            return
        # Any failure, not only an existing lock: on BeeGFS, a `mkdir` of a
        # name another node removes meanwhile can fail with ENOENT.
        except asyncssh.SFTPError:
            pass
        try:
            renewed = (await sftp.stat(lock)).mtime
        except asyncssh.SFTPNoSuchFile:
            # Released meanwhile.
            continue
        now = time.monotonic()
        if seen is None:
            logger.info(t"Waiting for another provisioner of the {what}.")
        if seen is None or seen[0] != renewed:
            seen = (renewed, now)
        elif now - seen[1] >= _LOCK_STALE_SECONDS:
            logger.warning(
                t"Breaking the lock {lock:path}: its holder hasn't renewed it for "
                t"{_LOCK_STALE_SECONDS:detail:g} seconds."
            )
            await sftp.rmtree(lock, ignore_errors=True)
            seen = None
            continue
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _renew(sftp: asyncssh.SFTPClient, lock: PurePosixPath) -> None:
    while True:
        await asyncio.sleep(_LOCK_RENEW_SECONDS)
        now = int(time.time())
        try:
            await sftp.utime(lock, (now, now))
        except asyncssh.SFTPError as error:
            logger.debug(t"Could not renew the lock {lock:path}: {error!r}")


async def _remove_leftovers(
    host: RemoteHost, sftp: asyncssh.SFTPClient, directory: PurePosixPath, key: str
) -> None:
    """
    Remove the key's attempts that aren't published, left by provisioners that
    failed or were killed. Only while holding the key's lock, so never one that
    is being built.
    """
    leftovers = [
        directory / name
        for name in await sftp.listdir(directory)
        if name.startswith(f"{key}.") and name != f"{key}{_LOCK_SUFFIX}"
    ]
    if leftovers:
        logger.info(
            t"Removing what failed provisioning attempts left: {leftovers:path}."
        )
        await _remove(host, leftovers)


async def _remove(host: RemoteHost, paths: Sequence[PurePosixPath]) -> None:
    """
    Remove `paths`, read-only ones included, for at most `_REMOVE_SECONDS`, even
    while being cancelled. Best effort: a failure is logged.
    """
    arguments = [str(path) for path in paths]
    script = 'chmod -R u+w -- "$@" 2>/dev/null; rm -rf -- "$@"'
    removing = asyncio.ensure_future(
        host.run(["sh", "-c", script, "remove", *arguments])
    )
    try:
        async with asyncio.timeout(_REMOVE_SECONDS):
            await asyncio.shield(removing)
    except asyncio.CancelledError:
        await asyncio.wait([removing], timeout=_REMOVE_SECONDS)
        raise
    except (TimeoutError, CommandFailed, asyncssh.Error, OSError) as error:
        logger.warning(t"Could not remove {list(paths):path}: {error!r}")


async def _free_space(host: RemoteHost, directory: PurePosixPath) -> str:
    """How much space is left where `directory` is, as `df` says, for an error."""
    try:
        output = await host.run(["df", "-Pk", str(directory)])
        available_kib = int(output.splitlines()[-1].split()[3])
    except (CommandFailed, asyncssh.Error, OSError, ValueError, IndexError) as error:
        logger.debug(t"Could not tell the free space: error={error!r}")
        return "A full disk or quota is a likely cause."
    gib = available_kib / 1024**2
    return (
        f"{gib:.1f} GiB is free on its filesystem; a full disk or quota is a likely "
        "cause, also when it seems enough."
    )


def _reason(error: BaseException) -> str:
    """What failed, in a line or a few: Pixi's last words, or the SFTP error."""
    if isinstance(error, CommandFailed):
        lines = [line for line in str(error.stderr).splitlines() if line.strip()]
        return "\n".join(lines[-10:]) or str(error)
    return str(error)


def _assets_builder(
    sftp: asyncssh.SFTPClient, files: Sequence[AssetFile], modes: _Modes
) -> Build:
    """
    Upload the asset files, then make them and their directories read-only, in
    `modes`.
    """

    async def build(attempt: PurePosixPath) -> None:
        directories = sorted(
            {attempt / parent for file in files for parent in file.relative.parents}
            - {attempt / "."},
            key=lambda path: len(path.parts),
        )
        for directory in directories:
            await _directory(sftp, directory, modes.building)
        uploads = asyncio.Semaphore(_UPLOADS_AT_ONCE)

        async def upload(file: AssetFile) -> None:
            async with uploads:
                await sftp.put(file.path, attempt / file.relative)
                await sftp.chmod(
                    attempt / file.relative,
                    modes.read_only(executable=file.executable),
                )

        # A group, so that when one upload fails the others have stopped before
        # the attempt is removed.
        try:
            async with asyncio.TaskGroup() as group:
                for file in files:
                    group.create_task(upload(file))
        except ExceptionGroup as failed:
            raise failed.exceptions[0] from None
        # Deepest first, while their parents are still writable.
        for directory in [*reversed(directories), attempt]:
            await sftp.chmod(directory, modes.read_only(executable=True))

    return build
