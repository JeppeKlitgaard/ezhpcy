"""
An app's environment and assets on its host (`ezhpcy.app.provision`), against
the in-process SSH server with a fake `pixi`.
"""

import asyncio
import logging
import os
import re
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

import asyncssh
import pytest

from ezhpcy import ssh
from ezhpcy.app import provision as provisioning
from ezhpcy.app.manifest import App, Manifest, assets_key, environment_key
from ezhpcy.app.provision import AppsLayout, provision_app, validate_app
from ezhpcy.provision_host import ProvisioningError
from ezhpcy.types import Login
from tests.support.apps import (
    MINIMAL_APP,
    PREFIX_MARKER,
    FakePixi,
    serve_shell_tools,
    with_root_mode,
    write_project,
)
from tests.support.login import CACHE, REMOTE_STATE
from tests.support.sshserver import SSHTestServer, reply

APPS_ROOT = CACHE / "ezhpcy-apps"
LAYOUT = AppsLayout(APPS_ROOT)
WITH_ASSETS = MINIMAL_APP + 'assets.include = ["main.py", "static/**"]\n'


@pytest.fixture
def fake_pixi(ssh_server: SSHTestServer) -> FakePixi:
    serve_shell_tools(ssh_server)
    return FakePixi(ssh_server)


def load_app(directory: Path, manifest: str, **project: object) -> App:
    [versions] = Manifest.load(
        write_project(directory, manifest, **project)
    ).apps.values()
    [app] = versions.values()
    return app


@pytest.fixture
def app(tmp_path: Path) -> App:
    return load_app(
        tmp_path / "project",
        WITH_ASSETS,
        files={
            "main.py": "print('hello')\r\n",
            "static/style.css": "body {}\n",
            "static/img/logo.svg": "<svg/>\n",
        },
    )


@asynccontextmanager
async def connected(server: SSHTestServer) -> AsyncGenerator[ssh.RemoteHost]:
    conn_info = Login(user=server.user, host=server.host)
    async with await ssh.connect(
        conn_info,
        port=server.port,
        client_keys=[server.client_key],
        agent_path=None,
        known_hosts=server.known_hosts,
    ) as host:
        yield host


async def provision(server: SSHTestServer, app: App) -> provisioning.ProvisionedApp:
    async with connected(server) as host:
        return await provision_app(host, REMOTE_STATE, LAYOUT, app)


def entries(server: SSHTestServer, directory: PurePosixPath) -> list[str]:
    local = server.local_path(directory)
    return sorted(path.name for path in local.iterdir()) if local.exists() else []


# Environments


async def test_the_first_run_installs_and_the_second_does_nothing(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    first = await provision(ssh_server, app)
    second = await provision(ssh_server, app)

    key = environment_key(app.environment)
    assert first == second
    assert len(fake_pixi.installs) == 1
    # EzHPCy's own Pixi, installed on the host first.
    assert fake_pixi.pixi_installs == 1
    assert ssh_server.local_path(REMOTE_STATE.pixi_executable()).exists()
    # Into the apps' own Pixi cache.
    assert str(APPS_ROOT / "pixi-cache") in fake_pixi.cache_dirs
    # In place, in `envs/<key>/`: no attempts, links or locks left.
    assert entries(ssh_server, LAYOUT.environments) == [key]
    assert first.environment == LAYOUT.environments / key
    assert first.pixi_manifest == first.environment / "pixi.toml"
    assert entries(ssh_server, first.environment) == [
        ".ezhpcy-installed",
        ".ezhpcy-last-used",
        ".pixi",
        "pixi.lock",
        "pixi.toml",
    ]
    assert fake_pixi.installs == [str(first.pixi_manifest)]
    install = next(argv for argv in ssh_server.commands_run if "install" in argv)
    assert install[4:] == [
        "install",
        "--frozen",
        "--environment",
        "default",
        "--manifest-path",
        str(first.pixi_manifest),
    ]
    # Its modes left as Pixi gives them: the apps root keeps others out.
    assert not [argv for argv in ssh_server.commands_run if argv[0] == "chmod"]


async def test_the_users_pixi_files_arrive_unchanged(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    provisioned = await provision(ssh_server, app)

    environment = ssh_server.local_path(provisioned.environment)
    for local in (app.environment.pixi, app.environment.lock):
        assert (environment / local.name).read_bytes() == local.read_bytes()


@pytest.mark.parametrize(
    "args",
    [
        ["lock"],
        ["update"],
        ["install", "--manifest-path", "pixi.toml"],
    ],
    ids=["lock", "update", "not-frozen"],
)
async def test_fake_pixi_refuses_what_would_write_the_users_files(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, args: list[str]
) -> None:
    async with connected(ssh_server) as host:
        with pytest.raises(ssh.CommandFailed, match="FakePixi refuses"):
            await host.run_pixi(args, REMOTE_STATE)


async def test_an_install_that_fails_partway_is_repaired_by_the_next(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    fake_pixi.fail = True

    with pytest.raises(ProvisioningError) as raised:
        await provision(ssh_server, app)

    message = str(raised.value)
    assert "No space left on device" in message
    assert "1.0 GiB is free" in message
    assert "full disk or quota" in message
    assert "The next attempt repairs what this one left" in message
    directory = LAYOUT.environments / environment_key(app.environment)
    # Left as it is, for Pixi to repair; not marked installed; not locked.
    assert entries(ssh_server, LAYOUT.environments) == [directory.name]
    assert ".ezhpcy-installed" not in entries(ssh_server, directory)
    assert (ssh_server.local_path(directory) / PREFIX_MARKER.lstrip("/")).exists()

    fake_pixi.fail = False
    provisioned = await provision(ssh_server, app)

    # In the same place.
    assert fake_pixi.installs == [str(provisioned.pixi_manifest)] * 2
    assert ".ezhpcy-installed" in entries(ssh_server, directory)


async def test_two_provisioners_at_once_install_once(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provisioning, "_LOCK_POLL_SECONDS", 0.05)
    installing = threading.Event()
    finish = threading.Event()

    def hold(_manifest: str) -> None:
        installing.set()
        # On the server's thread: the second provisioner waits meanwhile.
        finish.wait(5)

    fake_pixi.installing = hold
    first = asyncio.create_task(provision(ssh_server, app))
    assert await asyncio.to_thread(installing.wait, 5)
    second = asyncio.create_task(provision(ssh_server, app))
    await asyncio.sleep(0.3)
    assert not second.done()
    finish.set()

    assert await first == await second
    assert len(fake_pixi.installs) == 1


async def test_two_environments_install_one_at_a_time(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They share the apps root's Pixi cache, which file locks don't guard."""
    monkeypatch.setattr(provisioning, "_LOCK_POLL_SECONDS", 0.05)
    other = load_app(tmp_path / "other", MINIMAL_APP, pixi_toml="[workspace]\n")
    assert environment_key(other.environment) != environment_key(app.environment)
    installing = threading.Event()
    finish = threading.Event()

    def hold(_manifest: str) -> None:
        if not installing.is_set():
            installing.set()
            finish.wait(5)

    fake_pixi.installing = hold
    first = asyncio.create_task(provision(ssh_server, app))
    assert await asyncio.to_thread(installing.wait, 5)
    second = asyncio.create_task(provision(ssh_server, other))
    await asyncio.sleep(0.3)
    assert len(fake_pixi.installs) == 1
    finish.set()
    await asyncio.gather(first, second)

    assert len(fake_pixi.installs) == 2


async def test_a_lock_left_by_a_killed_provisioner_is_broken(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provisioning, "_LOCK_STALE_SECONDS", 0.3)
    monkeypatch.setattr(provisioning, "_LOCK_POLL_SECONDS", 0.05)
    # Never renewed again.
    ssh_server.local_path(LAYOUT.environments / ".install.lock").mkdir(parents=True)

    provisioned = await provision(ssh_server, app)

    assert entries(ssh_server, LAYOUT.environments) == [provisioned.environment.name]


async def test_a_changed_lock_file_is_a_new_environment(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    first = await provision(ssh_server, app)
    with app.environment.lock.open("a", encoding="utf-8") as file:
        file.write("# changed\n")

    second = await provision(ssh_server, app)

    assert first.environment != second.environment
    assert len(fake_pixi.installs) == 2


# Assets


async def test_assets_are_published_once_as_they_are_and_read_only(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    provisioned = await provision(ssh_server, app)
    again = await provision(ssh_server, app)

    assert provisioned.assets is not None
    assert again.assets == provisioned.assets
    key = assets_key(app.assets.files if app.assets else ())
    # Published as `<key>`, a symlink to its attempt's directory.
    assert entries(ssh_server, LAYOUT.assets) == sorted([key, provisioned.assets.name])
    assert ssh_server.local_path(LAYOUT.assets / key).is_symlink()
    assets = ssh_server.local_path(provisioned.assets)
    # Byte for byte: line endings left as they are.
    local = app.environment.pixi.parent / "main.py"
    assert (assets / "main.py").read_bytes() == local.read_bytes()
    assert b"\r\n" in local.read_bytes()
    assert (assets / "static" / "img" / "logo.svg").read_text() == "<svg/>\n"
    assert not os.access(assets / "main.py", os.W_OK)


async def test_every_directory_and_asset_is_its_owners_alone(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The modes asked of the server, whose filesystem here may not keep them.
    created: list[int | None] = []
    chmods: dict[str, int] = {}

    def recording_creation(name: str) -> None:
        create = getattr(asyncssh.SFTPClient, name)

        async def recording(
            self: asyncssh.SFTPClient, path: object, *args: object, **kwargs: object
        ) -> object:
            attrs = kwargs.get("attrs", args[0] if args else asyncssh.SFTPAttrs())
            assert isinstance(attrs, asyncssh.SFTPAttrs)
            created.append(attrs.permissions)
            return await create(self, path, *args, **kwargs)

        monkeypatch.setattr(asyncssh.SFTPClient, name, recording)

    recording_creation("mkdir")
    recording_creation("makedirs")
    chmod = asyncssh.SFTPClient.chmod

    async def recording_chmod(
        self: asyncssh.SFTPClient, path: str, mode: int, **kwargs: object
    ) -> None:
        chmods[PurePosixPath(path).name] = mode
        await chmod(self, path, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncssh.SFTPClient, "chmod", recording_chmod)

    provisioned = await provision(ssh_server, app)

    assert provisioned.assets is not None
    assert created
    assert set(created) == {0o700}
    assert chmods["main.py"] == 0o400
    assert chmods["logo.svg"] == 0o400
    assert chmods["img"] == chmods[provisioned.assets.name] == 0o500


async def test_an_upload_that_fails_partway_leaves_nothing(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put = asyncssh.SFTPClient.put
    uploads = 0

    async def failing_put(self: asyncssh.SFTPClient, *args: object, **kwargs: object):
        nonlocal uploads
        uploads += 1
        # After the environment's two Pixi files.
        if uploads == 4:
            raise asyncssh.SFTPFailure("No space left on device")
        return await put(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncssh.SFTPClient, "put", failing_put)

    with pytest.raises(ProvisioningError, match="No space left on device"):
        await provision(ssh_server, app)

    # The environment was installed; the assets left nothing.
    assert len(entries(ssh_server, LAYOUT.environments)) == 1
    assert entries(ssh_server, LAYOUT.assets) == []


async def test_an_attempt_left_by_a_killed_provisioner_is_removed(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provisioning, "_LOCK_STALE_SECONDS", 0.3)
    monkeypatch.setattr(provisioning, "_LOCK_POLL_SECONDS", 0.05)
    key = assets_key(app.assets.files if app.assets else ())
    leftover = ssh_server.local_path(LAYOUT.assets / f"{key}.0123456789abcdef")
    leftover.mkdir(parents=True)
    (leftover / "half-written").write_text("")
    # Its lock, never renewed again.
    ssh_server.local_path(LAYOUT.assets / f"{key}.lock").mkdir()

    provisioned = await provision(ssh_server, app)

    assert provisioned.assets is not None
    assert not leftover.exists()
    assert entries(ssh_server, LAYOUT.assets) == sorted([key, provisioned.assets.name])


async def test_a_provisioner_that_loses_the_race_to_publish_uses_the_winners(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = assets_key(app.assets.files if app.assets else ())
    winner = LAYOUT.assets / f"{key}.fedcba9876543210"
    symlink = asyncssh.SFTPClient.symlink

    def publish(entry: Path) -> None:
        ssh_server.local_path(winner).mkdir()
        entry.symlink_to(winner.name)

    async def publish_first(
        self: asyncssh.SFTPClient, old: object, new: PurePosixPath
    ) -> None:
        # Another provisioner, which took the lock for dead, publishes first.
        await asyncio.to_thread(publish, ssh_server.local_path(new))
        await symlink(self, old, new)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncssh.SFTPClient, "symlink", publish_first)

    provisioned = await provision(ssh_server, app)

    assert provisioned.assets is not None
    assert provisioned.assets.name == winner.name
    # Its own attempt is gone.
    assert entries(ssh_server, LAYOUT.assets) == sorted([key, winner.name])


# Checking without changing anything


async def test_validate_app_returns_where_they_are(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    provisioned = await provision(ssh_server, app)
    commands = len(ssh_server.commands_run)

    async with connected(ssh_server) as host:
        assert await validate_app(host, REMOTE_STATE, LAYOUT, app) == provisioned

    assert len(ssh_server.commands_run) == commands


async def test_validate_app_names_what_is_missing(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    async with connected(ssh_server) as host:
        with pytest.raises(ProvisioningError, match="isn't provisioned") as raised:
            await validate_app(host, REMOTE_STATE, LAYOUT, app)

    message = str(raised.value)
    assert str(REMOTE_STATE.pixi_executable()) in message
    assert f"{environment_key(app.environment)}/.ezhpcy-installed" in message
    assert assets_key(app.assets.files if app.assets else ()) in message


async def test_validate_app_refuses_an_unfinished_install(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    fake_pixi.fail = True
    with pytest.raises(ProvisioningError):
        await provision(ssh_server, app)

    async with connected(ssh_server) as host:
        with pytest.raises(ProvisioningError, match=re.escape(".ezhpcy-installed")):
            await validate_app(host, REMOTE_STATE, LAYOUT, app)


# Shared roots

SHARED = PurePosixPath("/dtu/qim/ezhpcy-apps")
OTHER_SHARED = PurePosixPath("/work3/admin/public/ezhpcy-apps")


async def install_shared(
    server: SSHTestServer,
    app: App,
    root: PurePosixPath,
    monkeypatch: pytest.MonkeyPatch,
    mode: int = 0o2775,
) -> provisioning.ProvisionedApp:
    """Install `app` in the shared root `root`, of `mode`, as an administrator."""
    server.local_path(root).mkdir(parents=True, exist_ok=True)
    with_root_mode(monkeypatch, root, mode)
    async with connected(server) as host:
        return await provision_app(
            host, REMOTE_STATE, AppsLayout(root), app, shared=True
        )


def tree(server: SSHTestServer, root: PurePosixPath) -> dict[str, int]:
    """Every path under `root`, with its modification time."""
    local = server.local_path(root)
    return {
        path.relative_to(local).as_posix(): path.lstat().st_mtime_ns
        for path in local.rglob("*")
    }


def recorded_chmods(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The modes provisioning sets from now on, by the name of what it sets."""
    chmods: dict[str, int] = {}
    chmod = asyncssh.SFTPClient.chmod

    async def recording_chmod(
        self: asyncssh.SFTPClient, path: str, mode: int, **kwargs: object
    ) -> None:
        chmods[PurePosixPath(path).name] = mode
        await chmod(self, path, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncssh.SFTPClient, "chmod", recording_chmod)
    return chmods


async def test_a_shared_roots_install_is_used_and_never_written(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = await install_shared(ssh_server, app, SHARED, monkeypatch)
    before = tree(ssh_server, SHARED)
    layout = AppsLayout(APPS_ROOT, (SHARED,))

    async with connected(ssh_server) as host:
        provisioned = await provision_app(host, REMOTE_STATE, layout, app)
        validated = await validate_app(host, REMOTE_STATE, layout, app)

    assert provisioned == validated == shared
    assert provisioned.environment.is_relative_to(SHARED)
    assert len(fake_pixi.installs) == 1
    # Not even marked used.
    assert tree(ssh_server, SHARED) == before
    assert entries(ssh_server, LAYOUT.environments) == []
    assert entries(ssh_server, LAYOUT.assets) == []


async def test_shared_roots_are_looked_in_in_order_for_the_environment_and_assets_apart(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await install_shared(ssh_server, app, OTHER_SHARED, monkeypatch)
    second = await install_shared(ssh_server, app, SHARED, monkeypatch)
    ssh_server.local_path(first.environment / ".ezhpcy-installed").unlink()

    async with connected(ssh_server) as host:
        provisioned = await provision_app(
            host, REMOTE_STATE, AppsLayout(APPS_ROOT, (OTHER_SHARED, SHARED)), app
        )

    assert provisioned.environment == second.environment
    assert provisioned.assets == first.assets
    assert len(fake_pixi.installs) == 2


async def test_what_no_shared_root_has_is_installed_in_the_apps_root(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    caplog: pytest.LogCaptureFixture,
) -> None:
    layout = AppsLayout(APPS_ROOT, (SHARED,))

    with caplog.at_level(logging.INFO, logger="ezhpcy.app.provision"):
        async with connected(ssh_server) as host:
            provisioned = await provision_app(host, REMOTE_STATE, layout, app)

    assert provisioned.environment.parent == LAYOUT.environments
    assert provisioned.assets is not None
    assert provisioned.assets.parent == LAYOUT.assets
    assert fake_pixi.installs == [str(provisioned.pixi_manifest)]
    assert "as no shared root has it" in caplog.text


async def test_a_shared_root_that_cant_be_read_is_passed_over(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await install_shared(ssh_server, app, SHARED, monkeypatch)
    for name in ("stat", "lstat"):
        original = getattr(asyncssh.SFTPClient, name)

        def denying(original: object = original) -> object:
            async def deny(
                self: asyncssh.SFTPClient, path: object, *args: object, **kwargs: object
            ) -> object:
                if PurePosixPath(str(path)).is_relative_to(SHARED):
                    raise asyncssh.SFTPPermissionDenied("Permission denied")
                return await original(self, path, *args, **kwargs)  # type: ignore[operator]

            return deny

        monkeypatch.setattr(asyncssh.SFTPClient, name, denying())

    provisioned = await provision(ssh_server, app)

    assert provisioned.environment.parent == LAYOUT.environments
    assert len(fake_pixi.installs) == 2


async def test_validate_app_names_every_place_it_looked_in(
    ssh_server: SSHTestServer, fake_pixi: FakePixi, app: App
) -> None:
    async with connected(ssh_server) as host:
        with pytest.raises(ProvisioningError, match="isn't provisioned") as raised:
            await validate_app(
                host, REMOTE_STATE, AppsLayout(APPS_ROOT, (SHARED,)), app
            )

    message = str(raised.value)
    environment = environment_key(app.environment)
    assets = assets_key(app.assets.files if app.assets else ())
    for root in (SHARED, APPS_ROOT):
        assert f"{root}/envs/{environment}/.ezhpcy-installed" in message
        assert f"{root}/assets/{assets}" in message


@pytest.mark.parametrize(
    ("mode", "building", "installed", "file", "read_only"),
    [
        (0o2775, 0o2770, "a+rX,g+w,o-w", 0o664, (0o444, 0o555)),
        (0o755, 0o700, "a+rX,go-w", 0o644, (0o444, 0o555)),
        (0o750, 0o700, "g+rX,g-w,o-rwx", 0o640, (0o440, 0o550)),
        (0o2770, 0o2770, "g+rwX,o-rwx", 0o660, (0o440, 0o550)),
    ],
    ids=["group-writes", "owner-writes", "group-reads", "group-only"],
)
async def test_a_shared_install_takes_the_roots_group_and_mode(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    building: int,
    installed: str,
    file: int,
    read_only: tuple[int, int],
) -> None:
    chmods = recorded_chmods(monkeypatch)

    shared = await install_shared(ssh_server, app, SHARED, monkeypatch, mode)

    assert shared.assets is not None
    assert chmods["envs"] == chmods["assets"] == mode
    # Only those who may write in the root may enter while Pixi installs, as
    # what it writes has its packages' modes, and Pixi's cache stays theirs.
    assert chmods[shared.environment.name] == chmods["pixi-cache"] == building
    command = ["chmod", "-R", installed, "--", str(shared.environment)]
    assert command in ssh_server.commands_run
    assert chmods[".ezhpcy-installed"] == file
    # Read-only, to their owner too.
    read_only_file, read_only_executable = read_only
    assert chmods["main.py"] == chmods["logo.svg"] == read_only_file
    assert chmods["img"] == chmods[shared.assets.name] == read_only_executable


@pytest.mark.parametrize(
    ("mode", "problem"),
    [
        (None, "Create it first"),
        (0o777, "never let others write there"),
        (0o711, "has mode 0711; give it one of"),
    ],
    ids=["missing", "others-write", "unsupported"],
)
async def test_a_shared_root_that_others_could_change_is_refused(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    mode: int | None,
    problem: str,
) -> None:
    if mode is not None:
        ssh_server.local_path(SHARED).mkdir(parents=True)
        with_root_mode(monkeypatch, SHARED, mode)

    async with connected(ssh_server) as host:
        with pytest.raises(ProvisioningError, match=problem):
            await provision_app(
                host, REMOTE_STATE, AppsLayout(SHARED), app, shared=True
            )

    assert fake_pixi.installs == []
    assert entries(ssh_server, SHARED) == []


async def test_a_pixi_cache_on_another_filesystem_is_warned_of(
    ssh_server: SSHTestServer,
    fake_pixi: FakePixi,
    app: App,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ssh_server.command("stat -c %d", reply(stdout="42\n43\n"))

    with caplog.at_level(logging.WARNING, logger="ezhpcy.app.provision"):
        await provision(ssh_server, app)

    assert "on another filesystem" in caplog.text
    assert "a copy of every package" in caplog.text
