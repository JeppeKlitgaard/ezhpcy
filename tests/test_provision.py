"""`provision` and `prune` end to end, against the in-process SSH server."""

import asyncio
import socket
from pathlib import Path, PurePosixPath

import asyncssh
import pytest
from rich.prompt import Confirm

from ezhpcy import ssh
from ezhpcy.config import local_files
from ezhpcy.constants import EZHPCY_VERSION, WORKER_HOST_ALIAS, WORKER_HOST_KEY_NAME
from ezhpcy.provision_host import ProvisioningError, validate_worker_infrastructure
from ezhpcy.provision_local import WorkerCredentials
from ezhpcy.ssh import CommandFailed
from ezhpcy.types import Login
from ezhpcy.versions import OPENSSH_MATCHSPEC, PIXI_INSTALLER_URL, PIXI_VERSION
from tests.support.apps import (
    LAYOUT_DIRECTORY,
    MINIMAL_APP,
    PIXI_DIRECTORY,
    FakePixi,
    serve_shell_tools,
    with_root_mode,
    write_project,
)
from tests.support.cli import invoke, use_settings
from tests.support.login import CACHE, MACHINE_ID, REMOTE_STATE, STATE
from tests.support.sshserver import SSHTestServer, reply

_LOGIN = ["--host", "login.example.com", "--user", "alice"]
_PACKAGE_CACHE = CACHE / "ezhpcy" / EZHPCY_VERSION
_PIXI_HOME = _PACKAGE_CACHE / "pixi" / PIXI_VERSION
# Named after the configured login host, not the test server's address.
_REMOTE_SSH_DIR = _PACKAGE_CACHE / "ssh" / MACHINE_ID / "alice@login.example.com"
_CREDENTIALS = WorkerCredentials(MACHINE_ID, "alice", "login.example.com")
_SHARED = PurePosixPath("/dtu/qim/ezhpcy-apps")
# Two versions of one app, another app, and a link.
_MANY_APPS = (
    MINIMAL_APP
    + """
[ezhpcy.app.lab."v2.0"]
environment.pixi = "pixi.toml"
run.command = ["jupyter", "lab", "--ServerApp.sock={{ socket }}"]
endpoint.transport = "socket"

[ezhpcy.app.board."v1.0"]
environment.pixi = "pixi.toml"
run.command = ["tensorboard", "--port", "{{ port }}"]
endpoint.transport = "tcp"

[ezhpcy.app.code."v1.0"]
access.url = "vscode://vscode-remote/ssh-remote+{{ alias }}/"
"""
)


def _commands(server: SSHTestServer, name: str) -> list[list[str]]:
    return [argv for argv in server.commands_run if argv[0] == name]


# provision


def test_provision_installs_pixi_openssh_and_the_worker_keys(
    login: SSHTestServer,
) -> None:
    login.command("bash -c", reply())
    login.command("env", reply())

    result = invoke(["provision", *_LOGIN, "--yes"])

    assert result.exit_code == 0, result
    assert "Success: Remote provisioning completed." in result.stdout
    [(_bash, _option, script)] = [
        argv for argv in _commands(login, "bash") if argv[1] == "-c"
    ]
    assert f"if [ ! -x {_PIXI_HOME}/bin/pixi ]" in script
    assert f"curl --fail --location --show-error --silent {PIXI_INSTALLER_URL}" in (
        script
    )
    assert f"PIXI_HOME={_PIXI_HOME} PIXI_VERSION={PIXI_VERSION}" in script

    cache_openssh, check_sshd = _commands(login, "env")
    assert cache_openssh[:4] == [
        "env",
        f"PIXI_HOME={_PIXI_HOME}",
        f"PIXI_CACHE_DIR={_PACKAGE_CACHE / 'pixi_cache' / PIXI_VERSION}",
        f"{_PIXI_HOME}/bin/pixi",
    ]
    assert cache_openssh[4:6] == ["exec", f"--spec={OPENSSH_MATCHSPEC}"]
    assert cache_openssh[-1] == "-V"

    for local, remote in (
        (_CREDENTIALS.host_key, _CREDENTIALS.remote_host_key(REMOTE_STATE)),
        (
            _CREDENTIALS.host_public_key,
            _CREDENTIALS.remote_host_public_key(REMOTE_STATE),
        ),
    ):
        assert login.local_path(remote).read_bytes() == local.read_bytes()
    client_key = _CREDENTIALS.client_public_key.read_text(encoding="ascii").split()
    assert "-t" in check_sshd
    assert f"HostKey={_REMOTE_SSH_DIR}/{WORKER_HOST_KEY_NAME}" in check_sshd
    assert (
        f"AuthorizedKeysCommand=/bin/echo {client_key[0]} {client_key[1]}" in check_sshd
    )
    host_key = _CREDENTIALS.host_public_key.read_text(encoding="ascii").split()
    assert _CREDENTIALS.known_hosts.read_text(encoding="utf-8") == (
        f"{WORKER_HOST_ALIAS} {host_key[0]} {host_key[1]}\n"
    )


def test_provision_is_repeatable(login: SSHTestServer) -> None:
    login.command("bash -c", reply())
    login.command("env", reply())
    remote_host_key = login.local_path(_REMOTE_SSH_DIR / WORKER_HOST_KEY_NAME)

    first = invoke(["provision", *_LOGIN, "--yes"])
    uploaded = remote_host_key.read_bytes()
    second = invoke(["provision", *_LOGIN, "--yes"])

    assert first.exit_code == 0, first
    assert second.exit_code == 0, second
    # The worker keeps its host key.
    assert remote_host_key.read_bytes() == uploaded


def test_provision_fails_with_the_remote_command_s_error(
    login: SSHTestServer,
) -> None:
    login.command(
        "bash -c", reply(stderr="curl: (6) Could not resolve host\n", exit_status=1)
    )

    result = invoke(["provision", *_LOGIN, "--yes"])

    assert result.exit_code == 1, result
    assert isinstance(result.exception, CommandFailed)
    assert str(result.exception) == (
        "Remote command failed (1): curl: (6) Could not resolve host"
    )


def test_ctrl_c_at_the_confirmation_exits_130(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(Confirm, "ask", interrupt)

    result = invoke(["provision", *_LOGIN])

    assert result.exit_code == 130, result
    assert _commands(login, "env") == []


def _apps_on(server: SSHTestServer) -> FakePixi:
    """Install EzHPCy's Pixi and app environments on `server`, as `FakePixi`."""
    serve_shell_tools(server)
    return FakePixi(server)


def test_provision_installs_the_apps_given_in_the_shared_app_root_given(
    login: SSHTestServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pixi = _apps_on(login)
    manifest = write_project(tmp_path / "project", MINIMAL_APP)
    login.local_path(_SHARED).mkdir(parents=True)
    with_root_mode(monkeypatch, _SHARED, 0o2775)

    result = invoke(
        ["provision", *_LOGIN, "--manifest", str(manifest), "--app", "lab"]
        + ["--shared-app-root", str(_SHARED), "--yes"]
    )

    assert result.exit_code == 0, result
    [install] = pixi.installs
    assert install.startswith(f"{_SHARED}/{PIXI_DIRECTORY}/envs/")
    # So that the root's group shares it, and only that.
    environment = str(PurePosixPath(install).parent)
    assert ["chmod", "-R", "a+rX,g+w,o-w", "--", environment] in login.commands_run
    assert "Success: App lab v1.0 is provisioned." in result.stdout
    assert "Success: Remote provisioning completed." in result.stdout


def test_provision_installs_an_app_only_where_no_shared_root_has_it(
    login: SSHTestServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pixi = _apps_on(login)
    manifest = str(write_project(tmp_path / "project", MINIMAL_APP))
    login.local_path(_SHARED).mkdir(parents=True)
    with_root_mode(monkeypatch, _SHARED, 0o755)
    connection = {"host": "login.example.com", "user": "alice"}
    use_settings(
        monkeypatch,
        {
            "profile": {
                "base": {
                    "connection": connection,
                    "site": {"shared_app_roots": [str(_SHARED)]},
                },
            },
        },
    )
    apps = ["--manifest", manifest, "--app", "lab", "--yes"]

    shared = invoke(["provision", "base", *apps, "--shared-app-root", str(_SHARED)])
    again = invoke(["provision", "base", *apps])

    assert (shared.exit_code, again.exit_code) == (0, 0)
    # Once, in the shared root.
    [install] = pixi.installs
    assert install.startswith(f"{_SHARED}/{PIXI_DIRECTORY}/envs/")


def test_provision_all_apps_installs_every_version_of_every_app(
    login: SSHTestServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pixi = _apps_on(login)
    manifest = write_project(tmp_path / "project", _MANY_APPS)
    login.local_path(_SHARED).mkdir(parents=True)
    with_root_mode(monkeypatch, _SHARED, 0o2775)

    result = invoke(
        ["provision", *_LOGIN, "--manifest", str(manifest), "--all-apps"]
        + ["--no-load-builtin-manifests", "--shared-app-root", str(_SHARED), "--yes"]
    )

    assert result.exit_code == 0, result
    provisioned = [
        line.removeprefix("Success: App ").removesuffix(" is provisioned.")
        for line in result.stdout.splitlines()
        if line.endswith(" is provisioned.")
    ]
    # Not the link, which has nothing to install.
    assert sorted(provisioned) == ["board v1.0", "lab v1.0", "lab v2.0"]
    # One environment, which they share.
    [install] = pixi.installs
    assert install.startswith(f"{_SHARED}/{PIXI_DIRECTORY}/envs/")


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        (["--shared-app-root", "/dtu/qim/ezhpcy-apps"], "none is chosen"),
        (["--app", "lab", "--shared-app-root", "qim"], "must be an absolute path"),
        (["--app", "lab", "--all-apps"], "chooses some"),
    ],
    ids=["no-app", "relative", "all-and-some"],
)
def test_provision_refuses_apps_it_cant_install_so(
    login: SSHTestServer, tmp_path: Path, arguments: list[str], problem: str
) -> None:
    manifest = write_project(tmp_path / "project", MINIMAL_APP)

    result = invoke(
        ["provision", *_LOGIN, "--manifest", str(manifest), *arguments, "--yes"]
    )

    assert result.exit_code != 0, result
    assert problem in result.stderr
    assert login.commands_run == []


# Connecting


def test_a_rejected_login_is_one_error_line(login: SSHTestServer) -> None:
    login.auth.client_keys = [asyncssh.generate_private_key("ssh-ed25519")]

    result = invoke(["provision", *_LOGIN, "--yes"])

    assert result.exit_code == 1, result
    assert result.exception is None
    assert result.stderr == (
        "Error: Could not connect to alice@login.example.com: The login node accepted "
        "no SSH key from this machine, and offers no password without one.\n"
    )


def test_a_rejected_host_key_is_one_error_line(login: SSHTestServer) -> None:
    local_files.login_known_hosts_file.unlink()

    result = invoke(["prune", *_LOGIN], stdin="n\n")

    assert result.exit_code == 1, result
    assert result.stderr.endswith(
        "Error: Could not connect to alice@login.example.com: Host key for 127.0.0.1 "
        "rejected by user.\n"
    )
    assert login.commands_run == []


def test_a_refused_connection_is_one_error_line(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        monkeypatch.setattr(login, "port", unused.getsockname()[1])

    result = invoke(["prune", *_LOGIN])

    assert result.exit_code == 1, result
    assert result.stderr.startswith(
        "Error: Could not connect to alice@login.example.com: "
    )
    assert result.stderr.count("\n") == 1


# prune


def _make_remote_dirs(server: SSHTestServer, *paths: PurePosixPath) -> None:
    for path in paths:
        server.local_path(path).mkdir(parents=True)


def _make_remote_files(server: SSHTestServer, *paths: PurePosixPath) -> None:
    for path in paths:
        local_path = server.local_path(path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.touch()


def test_prune_removes_other_versions_only(login: SSHTestServer) -> None:
    login.command("sh", reply())
    pixi_cache = _PACKAGE_CACHE / "pixi_cache"
    _make_remote_dirs(
        login,
        _PACKAGE_CACHE,
        CACHE / "ezhpcy" / "0.0.9",
        CACHE / "ezhpcy" / "keep me",
        STATE / "ezhpcy" / "0.0.9",
        _PIXI_HOME,
        _PACKAGE_CACHE / "pixi" / "0.72.0",
        pixi_cache / PIXI_VERSION,
        pixi_cache / "0.72.0",
    )

    result = invoke(["prune", *_LOGIN])

    assert result.exit_code == 0, result
    # Read-only files too, as apps' assets are.
    [command] = _commands(login, "sh")
    assert "chmod -R u+w" in command[2]
    assert command[3:] == [
        "prune",
        f"{CACHE}/ezhpcy/0.0.9",
        f"{STATE}/ezhpcy/0.0.9",
        f"{_PACKAGE_CACHE}/pixi/0.72.0",
        f"{pixi_cache}/0.72.0",
    ]


def test_prune_removes_other_layouts_and_pixi_versions_from_the_apps_root(
    login: SSHTestServer,
) -> None:
    login.command("sh", reply())
    apps = CACHE / "ezhpcy-apps"
    layout = apps / LAYOUT_DIRECTORY
    _make_remote_dirs(
        login,
        apps / PIXI_DIRECTORY / "envs",
        layout / "pixi-0.72.0" / "envs",
        layout / "cache",
        layout / "assets",
        apps / "v0" / "envs",
        # What came before layouts, such as workdirs, which may hold the user's
        # work: left to `prune --all`.
        apps / "work" / "lab",
    )

    result = invoke(["prune", *_LOGIN])

    assert result.exit_code == 0, result
    [command] = _commands(login, "sh")
    assert command[3:] == ["prune", f"{apps}/v0", f"{layout}/pixi-0.72.0"]


def test_prune_with_nothing_on_the_server_removes_nothing(
    login: SSHTestServer,
) -> None:
    result = invoke(["prune", *_LOGIN])

    assert result.exit_code == 0, result
    assert _commands(login, "sh") == []


def test_prune_all_removes_the_package_cache_and_state_and_the_apps_root(
    login: SSHTestServer,
) -> None:
    login.command("sh", reply())

    result = invoke(["prune", *_LOGIN, "--all", "--yes"])

    assert result.exit_code == 0, result
    [command] = _commands(login, "sh")
    # Read-only assets are made writable first.
    assert command[:2] == ["sh", "-c"]
    assert "chmod -R u+w" in command[2]
    assert command[3:] == [
        "prune",
        f"{CACHE}/ezhpcy",
        f"{STATE}/ezhpcy",
        f"{CACHE}/ezhpcy-apps",
    ]
    assert "Success: All managed remote data removed." in result.stdout


def test_prune_all_says_it_keeps_the_apps_workdirs(login: SSHTestServer) -> None:
    result = invoke(["prune", *_LOGIN, "--all"], stdin="n\n")

    assert result.exit_code == 1, result
    assert "workdirs stay" in result.stderr
    assert "ezhpcy-apps" in result.stderr


def test_prune_all_with_workdirs_removes_them_too(login: SSHTestServer) -> None:
    login.command("sh", reply())

    asked = invoke(["prune", *_LOGIN, "--all", "--workdirs"], stdin="n\n")
    result = invoke(["prune", *_LOGIN, "--all", "--workdirs", "--yes"])

    assert "$XDG_STATE_HOME/ezhpcy-apps" in asked.stderr
    assert result.exit_code == 0, result
    [command] = _commands(login, "sh")
    assert command[3:] == [
        "prune",
        f"{CACHE}/ezhpcy",
        f"{STATE}/ezhpcy",
        f"{CACHE}/ezhpcy-apps",
        f"{STATE}/ezhpcy-apps",
    ]


def test_prune_workdirs_needs_all(login: SSHTestServer) -> None:
    result = invoke(["prune", *_LOGIN, "--workdirs"])

    assert result.exit_code != 0, result
    assert "--all" in result.stderr
    assert login.commands_run == []


def test_prune_keeps_the_apps_roots_current_installs(login: SSHTestServer) -> None:
    login.command("sh", reply())
    # A sibling of the package cache root, not inside it.
    _make_remote_dirs(login, _PACKAGE_CACHE, CACHE / "ezhpcy-apps" / PIXI_DIRECTORY)

    result = invoke(["prune", *_LOGIN])

    assert result.exit_code == 0, result
    assert _commands(login, "sh") == []


# validate_worker_infrastructure


async def _validate(server: SSHTestServer) -> None:
    conn_info = Login(user=server.user, host=server.host)
    async with await ssh.connect(
        conn_info,
        port=server.port,
        client_keys=[server.client_key],
        agent_path=None,
        known_hosts=server.known_hosts,
    ) as login:
        await validate_worker_infrastructure(login, REMOTE_STATE, _CREDENTIALS)


async def test_validate_worker_infrastructure_accepts_a_provisioned_host(
    ssh_server: SSHTestServer,
) -> None:
    await asyncio.to_thread(
        _make_remote_files,
        ssh_server,
        _PIXI_HOME / "bin" / "pixi",
        _REMOTE_SSH_DIR / WORKER_HOST_KEY_NAME,
        _REMOTE_SSH_DIR / f"{WORKER_HOST_KEY_NAME}.pub",
    )

    await _validate(ssh_server)

    assert ssh_server.commands_run == []


async def test_validate_worker_infrastructure_names_what_is_missing(
    ssh_server: SSHTestServer,
) -> None:
    with pytest.raises(ProvisioningError, match="run 'ezhpcy provision'") as raised:
        await _validate(ssh_server)

    assert f"{_PIXI_HOME}/bin/pixi" in str(raised.value)
    assert f"{_REMOTE_SSH_DIR}/{WORKER_HOST_KEY_NAME}.pub" in str(raised.value)
