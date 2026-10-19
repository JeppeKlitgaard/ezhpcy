import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ezhpcy.config import LocalFileConfig
from ezhpcy.messages import plain
from ezhpcy.provision_local import WorkerCredentials
from ezhpcy.tunnel import ssh_config
from ezhpcy.tunnel.lock import alias_lock
from ezhpcy.tunnel.ssh_config import (
    PROBE_HOST_NAME,
    WorkerHost,
    check_host_resolution,
    include_directive,
    probe_hosts,
    prune_stale_active_configs,
    write_active_host_config,
)

PORT = 50022


def worker_host(alias: str = "gpu", user: str = "alice") -> WorkerHost:
    return WorkerHost.for_endpoint(alias, user=user, host="login.example.com")


def config_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "ssh-config"


def test_render_points_openssh_at_the_forwarded_port(
    monkeypatch: pytest.MonkeyPatch,
    isolated_local_files: LocalFileConfig,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        isolated_local_files, "config_dir", tmp_path / "config with spaces"
    )
    ssh_dir = (
        tmp_path / "config with spaces" / "ssh" / "machine" / "alice@login.example.com"
    )
    host = WorkerHost("gpu", WorkerCredentials("machine", "alice", "login.example.com"))

    assert host.render(PORT) == (
        "Host gpu\n"
        "    HostName 127.0.0.1\n"
        "    Port 50022\n"
        "    User alice\n"
        f'    IdentityFile "{(ssh_dir / "worker_client_ed25519").resolve().as_posix()}"\n'
        "    IdentitiesOnly yes\n"
        f'    UserKnownHostsFile "{(ssh_dir / "worker_known_hosts").resolve().as_posix()}"\n'
        "    HostKeyAlias ezhpcy-worker\n"
        "    StrictHostKeyChecking yes\n"
        "    ProxyCommand none\n"
    )


@pytest.mark.parametrize("alias", ["", "two words", "wild*", "neg!ated", "a,b", "-x"])
def test_worker_host_rejects_aliases_openssh_would_treat_as_patterns(
    alias: str,
) -> None:
    with pytest.raises(ValueError, match="may contain only"):
        worker_host(alias)


def test_worker_host_rejects_a_user_with_whitespace() -> None:
    with pytest.raises(ValueError, match="one non-whitespace token"):
        worker_host(user="alice smith")


def test_include_directive_is_a_commented_glob_over_the_runtime_directory(
    isolated_runtime_dir: Path,
) -> None:
    directory = config_dir(isolated_runtime_dir).resolve().as_posix()

    assert include_directive() == (
        f'# Load EzHPCy static and dynamic SSH profiles\nInclude "{directory}/*.conf"'
    )


def test_active_config_holds_the_tunnels_block(isolated_runtime_dir: Path) -> None:
    path = write_active_host_config(worker_host("scratch"), PORT)

    assert path == config_dir(isolated_runtime_dir) / "active-scratch.conf"
    assert path.read_text(encoding="utf-8") == worker_host("scratch").render(PORT)


def test_files_are_restricted_before_the_include_glob_can_match_them(
    isolated_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted: list[Path] = []

    def restrict(path: Path) -> None:
        assert not path.match("*.conf")
        assert not (config_dir(isolated_runtime_dir) / "active-gpu.conf").exists()
        restricted.append(path)

    monkeypatch.setattr(ssh_config, "restrict_to_current_user", restrict)

    write_active_host_config(worker_host(), PORT)

    assert len(restricted) == 1
    assert [path.name for path in config_dir(isolated_runtime_dir).iterdir()] == [
        "active-gpu.conf"
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_generated_files_are_owner_only_on_posix() -> None:
    path = write_active_host_config(worker_host(), PORT)

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_withdraw_removes_the_block() -> None:
    path = write_active_host_config(worker_host(), PORT)

    ssh_config.withdraw_host(worker_host())

    assert not path.exists()


def test_withdrawing_an_absent_block_is_a_no_op() -> None:
    ssh_config.withdraw_host(worker_host())


def test_prune_keeps_only_the_blocks_of_running_tunnels() -> None:
    live = write_active_host_config(worker_host("live"), PORT)
    # Left by a tunnel that was killed: no one holds its lock.
    stale = write_active_host_config(worker_host("gone"), PORT + 1)

    with alias_lock("live"):
        removed = prune_stale_active_configs()

    assert removed == [stale]
    assert live.exists()


def test_prune_removes_a_block_whose_name_no_tunnel_writes(
    isolated_runtime_dir: Path,
) -> None:
    odd = config_dir(isolated_runtime_dir) / "active-.conf"
    odd.parent.mkdir(parents=True)
    odd.write_text("")

    assert prune_stale_active_configs() == [odd]


def test_prune_without_a_config_directory_is_a_no_op() -> None:
    assert prune_stale_active_configs() == []


def ssh_g_output(host: WorkerHost, port: int = PORT, **overrides: str) -> str:
    # `ssh -G` prints no `proxycommand` for `ProxyCommand none`.
    fields = {
        "user": host.user,
        "hostname": "127.0.0.1",
        "port": str(port),
        "identityfile": host.identity_file.resolve().as_posix(),
        **overrides,
    }
    return "".join(f"{key} {value}\n" for key, value in fields.items())


def fake_ssh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    error: Exception | None = None,
) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if error is not None:
            raise error
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(ssh_config.subprocess, "run", run)
    return commands


def test_host_resolving_to_the_generated_block_has_no_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = worker_host()
    commands = fake_ssh(monkeypatch, stdout=ssh_g_output(host))

    assert check_host_resolution(host, PORT) is None
    assert commands == [["ssh", "-G", "gpu"]]


def test_an_explicit_proxycommand_none_has_no_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = worker_host()
    fake_ssh(monkeypatch, stdout=ssh_g_output(host, proxycommand="none"))

    assert check_host_resolution(host, PORT) is None


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"hostname": "gpu.example.com"}, "does not resolve it to the ezhpcy"),
        ({"port": "22"}, "does not resolve it to the ezhpcy"),
        ({"proxycommand": "nc gpu 22"}, "ProxyCommand: nc gpu 22"),
        ({"user": "bob"}, "different user"),
        ({"identityfile": "~/.ssh/id_ed25519"}, "different IdentityFile"),
    ],
)
def test_host_shadowed_by_other_configuration_is_reported(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str], problem: str
) -> None:
    host = worker_host()
    fake_ssh(monkeypatch, stdout=ssh_g_output(host, **overrides))

    assert problem in (check_host_resolution(host, PORT) or "")


def test_ssh_refusing_the_configuration_is_reported_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ssh(
        monkeypatch,
        returncode=255,
        stderr="Bad owner or permissions on\n  /x/active-gpu.conf\n",
    )

    assert check_host_resolution(worker_host(), PORT) == (
        "Bad owner or permissions on /x/active-gpu.conf"
    )


def test_missing_ssh_skips_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ssh(monkeypatch, error=FileNotFoundError("ssh"))

    assert check_host_resolution(worker_host(), PORT) is None


def test_hanging_ssh_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ssh(monkeypatch, error=subprocess.TimeoutExpired(["ssh"], 10))

    assert "could not run `ssh -G gpu`" in (
        check_host_resolution(worker_host(), PORT) or ""
    )


def test_publish_writes_the_block_and_withdraw_removes_it(
    isolated_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[tuple[str, int]] = []
    monkeypatch.setattr(
        ssh_config,
        "check_host_resolution",
        lambda host, port: checked.append((host.alias, port)),
    )
    active = config_dir(isolated_runtime_dir) / "active-gpu.conf"

    ssh_config.publish_host(worker_host(), PORT)

    assert active.read_text(encoding="utf-8") == worker_host().render(PORT)
    assert checked == [("gpu", PORT)]
    ssh_config.withdraw_host(worker_host())
    assert not active.exists()


def test_publish_prunes_stale_blocks_but_not_its_own(
    isolated_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_config, "check_host_resolution", lambda _host, _port: None)
    stale = write_active_host_config(worker_host("gone"), PORT + 1)

    # As the tunnel does: it holds its alias's lock while it serves.
    with alias_lock("gpu"):
        ssh_config.publish_host(worker_host(), PORT)
        assert not stale.exists()

        # The next tunnel start's pruning keeps it.
        prune_stale_active_configs()
        assert (config_dir(isolated_runtime_dir) / "active-gpu.conf").exists()


def test_publish_explains_how_to_include_the_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        ssh_config, "check_host_resolution", lambda _host, _port: "not ours"
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        ssh_config.logger,
        "warning",
        lambda message: warnings.append(plain(message)),
    )

    ssh_config.publish_host(worker_host(), PORT)

    assert warnings and "'ssh gpu' will not reach this tunnel: not ours" in warnings[0]
    stderr_lines = capsys.readouterr().err.splitlines()
    assert stderr_lines[1:3] == include_directive().splitlines()


def test_publish_failure_is_a_warning_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(ssh_config, "write_active_host_config", fail)
    warnings: list[str] = []
    monkeypatch.setattr(
        ssh_config.logger,
        "warning",
        lambda message: warnings.append(plain(message)),
    )

    ssh_config.publish_host(worker_host(), PORT)

    assert warnings == [
        "Could not write the SSH configuration for gpu: read-only file system"
    ]


def test_probe_hosts_exist_only_for_the_block(isolated_runtime_dir: Path) -> None:
    probe = config_dir(isolated_runtime_dir) / "doctor-probe.conf"

    with probe_hosts(["cpu", "gpu"]):
        content = probe.read_text(encoding="utf-8")

    assert f"Host cpu\n    HostName {PROBE_HOST_NAME}\n" in content
    assert f"Host gpu\n    HostName {PROBE_HOST_NAME}\n" in content
    assert not probe.exists()


requires_openssh = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="OpenSSH client is not installed"
)


def windows_acl_report() -> str:
    """Describe the owners and ACLs OpenSSH saw, to diagnose a rejected Include."""
    if os.name != "nt":
        return ""
    directory = ssh_config.ssh_config_dir()
    commands = [
        ["whoami", "/user", "/groups"],
        ["icacls", str(directory)],
        *(["icacls", str(path)] for path in sorted(directory.iterdir())),
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"Get-ChildItem -Force '{directory}' | Get-Acl | Format-List Path, Owner",
        ],
    ]
    outputs = (
        subprocess.run(command, capture_output=True, check=False, text=True).stdout
        for command in commands
    )
    return "\n" + "\n".join(outputs)


def resolve_with_openssh(
    tmp_path: Path, alias: str, after_include: str = ""
) -> dict[str, str]:
    """
    Resolve an alias through the real OpenSSH client, with only our Include and
    then `after_include`.
    """
    main_config = tmp_path / "ssh_config"
    main_config.write_text(include_directive() + "\n" + after_include, encoding="utf-8")
    result = subprocess.run(
        ["ssh", "-F", str(main_config), "-G", alias],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr + windows_acl_report()
    resolved: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        resolved.setdefault(key, value)
    return resolved


@requires_openssh
def test_openssh_accepts_the_generated_files_through_the_include(
    tmp_path: Path,
) -> None:
    # Real permissions are applied here, so this also proves OpenSSH accepts the
    # ACL or mode bits that restrict_to_current_user sets.
    write_active_host_config(worker_host("gpu"), PORT)

    resolved = resolve_with_openssh(tmp_path, "gpu")

    assert (resolved["hostname"], resolved["port"]) == ("127.0.0.1", str(PORT))
    assert resolved["user"] == "alice"
    assert "proxycommand" not in resolved


@requires_openssh
def test_openssh_ignores_a_later_proxy_jump_for_the_block(tmp_path: Path) -> None:
    write_active_host_config(worker_host("gpu"), PORT)

    resolved = resolve_with_openssh(
        tmp_path, "gpu", "Host *\n    ProxyJump jump.example.com\n"
    )

    assert "proxyjump" not in resolved
    assert resolved["hostname"] == "127.0.0.1"


@requires_openssh
def test_openssh_prefers_a_running_tunnels_block_over_the_probe(
    tmp_path: Path,
) -> None:
    write_active_host_config(worker_host("gpu"), PORT)

    with probe_hosts(["gpu", "cpu"]):
        gpu = resolve_with_openssh(tmp_path, "gpu")
        cpu = resolve_with_openssh(tmp_path, "cpu")

    assert gpu["hostname"] == "127.0.0.1"
    assert cpu["hostname"] == PROBE_HOST_NAME
