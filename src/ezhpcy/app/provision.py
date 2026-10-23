"""
An app's environment and assets on the host it runs on, under the apps root
(by default `<XDG cache>/ezhpcy-apps` there, see `RemoteState.apps_root`):

    <apps root>/
      envs/<key>/              the Pixi files, and Pixi's `.pixi/`
      envs/<key>/.ezhpcy-installed   once `pixi install --frozen` succeeded
      envs/.install.lock/      held while an environment installs
      assets/<key>             symlink to <key>.<attempt>, once that attempt completed
      assets/<key>.<attempt>/  the asset files, read-only: one attempt
      assets/<key>.lock/       held while an attempt runs
      pixi-cache/              Pixi's cache for app environments
      work/<normalised name>/  the default workdir: the user's, never pruned

**Environments** are installed by EzHPCy's own Pixi, in place (decision 42 of
PLAN.md, and FINDINGS.md's "Phase 4"): Pixi detects and repairs an install
that was interrupted, and an environment keyed by its Pixi files never goes out
of date. An install holds the apps root's one lock, as file locks don't hold
across a cluster's nodes, so that Pixi's shared cache is never written by two
installs at once; on success it writes `.ezhpcy-installed`. A run uses only an
environment with that marker, and never installs (`pixi run --as-is`).

**Assets** are built once by `publish_once`: each attempt builds in a fresh
directory of its own, and the entry appears under its key only once the attempt
has succeeded, as a symlink created in one atomic step. An attempt that failed,
typically on a full disk or quota, is removed, and never used or built over:
the next one removes any leftovers, while holding the key's lock, and starts
afresh.

The locks are directories, as `mkdir` is atomic, on NFS and BeeGFS too.
"""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
lazy import asyncio
lazy import secrets
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
lazy from ezhpcy.provision_host import ProvisioningError, provision_pixi
lazy from ezhpcy.ssh import CommandFailed, RemoteHost, ensure_directory
lazy from ezhpcy.types import RemoteState

logger = get_logger(__name__)

ENVIRONMENTS_DIRECTORY = "envs"
ASSETS_DIRECTORY = "assets"
PIXI_CACHE_DIRECTORY = "pixi-cache"
WORK_DIRECTORY = "work"
# In an environment's directory, once its install succeeded.
INSTALLED_NAME = ".ezhpcy-installed"
# Touched in an entry's directory by each run that uses it, for pruning by age.
LAST_USED_NAME = ".ezhpcy-last-used"
_LOCK_SUFFIX = ".lock"
# The one lock of an apps root's installs, in `envs/`.
_INSTALL_LOCK_NAME = f".install{_LOCK_SUFFIX}"

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
    """Where an apps root keeps what (see the module's docstring)."""

    root: PurePosixPath

    @property
    def environments(self) -> PurePosixPath:
        return self.root / ENVIRONMENTS_DIRECTORY

    @property
    def assets(self) -> PurePosixPath:
        return self.root / ASSETS_DIRECTORY

    @property
    def pixi_cache(self) -> PurePosixPath:
        return self.root / PIXI_CACHE_DIRECTORY

    def environment(self, environment: Environment) -> PurePosixPath:
        """The directory of `environment`, by its key."""
        return self.environments / environment_key(environment)

    def workdir(self, app: App) -> PurePosixPath:
        """The app's workdir: its own `run.workdir`, or a default by its name."""
        return app.run.workdir or self.root / WORK_DIRECTORY / app.normalised_name


@dataclass(frozen=True, slots=True)
class ProvisionedApp:
    """
    Where an app's installed environment and published assets are: the Pixi
    manifest to run it by, and the assets' real path.
    """

    pixi_manifest: PurePosixPath
    assets: PurePosixPath | None

    @property
    def environment(self) -> PurePosixPath:
        """The environment's directory."""
        return self.pixi_manifest.parent


# Builds an attempt in the directory it is given; raises if it can't.
type Build = Callable[[PurePosixPath], Awaitable[None]]


async def provision_app(
    host: RemoteHost, remote_state: RemoteState, layout: AppsLayout, app: App
) -> ProvisionedApp:
    """
    Install EzHPCy's Pixi on `host` if it is missing, then the app's
    environment, and upload its assets, each unless done already. Raises
    `ProvisioningError`.
    """
    await provision_pixi(host, remote_state)
    async with host.sftp() as sftp:
        directory = await _install(host, sftp, remote_state, layout, app)
        assets = None
        if app.assets is not None:
            files = app.assets.files
            assets = await publish_once(
                host,
                sftp,
                layout.assets,
                assets_key(files),
                _assets_builder(sftp, files),
                what=f"assets of app {app}",
            )
    return ProvisionedApp(directory / app.environment.pixi.name, assets)


async def validate_app(
    host: RemoteHost, remote_state: RemoteState, layout: AppsLayout, app: App
) -> ProvisionedApp:
    """
    Where the app's environment and assets are, without changing anything;
    raises `ProvisioningError` if EzHPCy's Pixi, the environment or the assets
    aren't there for the app's current files.
    """
    directory = layout.environment(app.environment)
    missing: list[PurePosixPath] = []
    assets = None
    async with host.sftp() as sftp:
        for path in (remote_state.pixi_executable(), directory / INSTALLED_NAME):
            if not await sftp.exists(path):
                missing.append(path)
        if app.assets is not None:
            entry = layout.assets / assets_key(app.assets.files)
            if (assets := await _published(sftp, entry)) is None:
                missing.append(entry)
    if missing:
        raise ProvisioningError(
            t"app {str(app):name} isn't provisioned on this host for its current "
            t"files (missing: {missing:path})"
        )
    return ProvisionedApp(directory / app.environment.pixi.name, assets)


async def mark_used(sftp: asyncssh.SFTPClient, provisioned: ProvisionedApp) -> None:
    """
    Note that a run uses the environment and assets now, for pruning by age:
    by the time of their `LAST_USED_NAME` file, which their owner may set even
    where it can't write, as in the read-only assets.
    """
    now = int(time.time())
    for directory in (provisioned.environment, provisioned.assets):
        if directory is None:
            continue
        path = directory / LAST_USED_NAME
        try:
            await sftp.utime(path, (now, now))
        except asyncssh.SFTPNoSuchFile:
            try:
                async with sftp.open(path, "w"):
                    pass
            except asyncssh.SFTPError as error:
                logger.debug(t"Could not mark {directory:path} used: {error!r}")


async def _install(
    host: RemoteHost,
    sftp: asyncssh.SFTPClient,
    remote_state: RemoteState,
    layout: AppsLayout,
    app: App,
) -> PurePosixPath:
    """
    The directory of the app's environment, installing it first unless it is
    installed (see the module's docstring).
    """
    environment = app.environment
    directory = layout.environment(environment)
    installed = directory / INSTALLED_NAME
    what = f"environment of app {app}"
    if await sftp.exists(installed):
        logger.debug(t"Using the installed {what}: directory={directory:path}")
        return directory
    await ensure_directory(sftp, layout.environments)
    async with _held(sftp, layout.environments / _INSTALL_LOCK_NAME, what=what):
        # Another provisioner may have installed it while this one waited.
        if await sftp.exists(installed):
            logger.debug(t"Using the {what}, installed meanwhile.")
            return directory
        logger.info(t"Installing the {what} in {directory:path}.")
        started = time.monotonic()
        try:
            await ensure_directory(sftp, directory)
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
            async with sftp.open(installed, "w"):
                pass
        except (CommandFailed, asyncssh.Error, OSError) as error:
            free = await _free_space(host, directory)
            raise ProvisioningError(
                t"Could not install the {what}: {_reason(error)}. {free} The next "
                t"attempt repairs what this one left."
            ) from error
        elapsed = time.monotonic() - started
        logger.info(t"Installed the {what} in {elapsed:detail:.0f} seconds.")
    return directory


async def publish_once(
    host: RemoteHost,
    sftp: asyncssh.SFTPClient,
    directory: PurePosixPath,
    key: str,
    build: Build,
    *,
    what: str,
) -> PurePosixPath:
    """
    The real path of `directory / key`, building it first with `build` unless it
    is published already (see the module's docstring).

    Raises `ProvisioningError` if the build fails, saying how much space is left,
    as a full disk or quota is the likely cause, after removing what it left.
    """
    entry = directory / key
    if (published := await _published(sftp, entry)) is not None:
        logger.debug(t"Using published {what}: entry={entry:path}")
        return published
    await ensure_directory(sftp, directory)
    async with _held(sftp, directory / f"{key}{_LOCK_SUFFIX}", what=what):
        # Another provisioner may have published it while this one waited.
        if (published := await _published(sftp, entry)) is not None:
            logger.debug(t"Using {what}, published meanwhile: entry={entry:path}")
            return published
        await _remove_leftovers(host, sftp, directory, key)
        attempt = directory / f"{key}.{secrets.token_hex(8)}"
        logger.info(t"Provisioning the {what} in {attempt:path}.")
        started = time.monotonic()
        await sftp.mkdir(attempt)
        try:
            # First, while the attempt's directory is writable (see `mark_used`).
            async with sftp.open(attempt / LAST_USED_NAME, "w"):
                pass
            await build(attempt)
            await sftp.symlink(attempt.name, entry)
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
        return await _published(sftp, entry) or attempt


async def _published(
    sftp: asyncssh.SFTPClient, entry: PurePosixPath
) -> PurePosixPath | None:
    """The real path `entry` points at, if it is published: a symlink that resolves."""
    try:
        if (await sftp.lstat(entry)).type != asyncssh.FILEXFER_TYPE_SYMLINK:
            return None
        await sftp.stat(entry)
    except asyncssh.SFTPNoSuchFile:
        return None
    return PurePosixPath(str(await sftp.realpath(entry)))


@asynccontextmanager
async def _held(
    sftp: asyncssh.SFTPClient, lock: PurePosixPath, *, what: str
) -> AsyncGenerator[None]:
    """
    Hold `lock`, a directory, as `mkdir` creates it atomically, and renew it
    while held. Waits while another provisioner holds it, unless its holder
    hasn't renewed it within `_LOCK_STALE_SECONDS`, and is taken for dead.
    """
    await _acquire(sftp, lock, what=what)
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
    sftp: asyncssh.SFTPClient, lock: PurePosixPath, *, what: str
) -> None:
    # The lock's last renewal seen, and when it was seen to change, by this clock.
    seen: tuple[int | None, float] | None = None
    while True:
        try:
            await sftp.mkdir(lock, attrs=asyncssh.SFTPAttrs(permissions=0o700))
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
    # An entry whose attempt is gone can't be used, and would keep a new one from
    # being published.
    try:
        await sftp.lstat(directory / key)
        leftovers.append(directory / key)
    except asyncssh.SFTPNoSuchFile:
        pass
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


def _assets_builder(sftp: asyncssh.SFTPClient, files: Sequence[AssetFile]) -> Build:
    """Upload the asset files, then make them and their directories read-only."""

    async def build(attempt: PurePosixPath) -> None:
        directories = sorted(
            {attempt / parent for file in files for parent in file.relative.parents}
            - {attempt / "."},
            key=lambda path: len(path.parts),
        )
        for directory in directories:
            await sftp.mkdir(directory)
        uploads = asyncio.Semaphore(_UPLOADS_AT_ONCE)

        async def upload(file: AssetFile) -> None:
            async with uploads:
                await sftp.put(file.path, attempt / file.relative)
                await sftp.chmod(
                    attempt / file.relative, 0o555 if file.executable else 0o444
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
            await sftp.chmod(directory, 0o555)

    return build
