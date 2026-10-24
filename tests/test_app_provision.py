"""
An app's environment and assets on its host (`ezhpcy.app.provision`), against
the in-process SSH server with a fake `pixi`.
"""

import asyncio
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
    write_project,
)
from tests.support.login import CACHE, REMOTE_STATE
from tests.support.sshserver import SSHTestServer

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
