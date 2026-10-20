from collections.abc import Callable
from pathlib import Path

import pytest

from ezhpcy.cli import doctor as doctor_cli
from ezhpcy.config import config
from ezhpcy.tunnel import ssh_config
from ezhpcy.tunnel.lock import alias_lock
from ezhpcy.tunnel.ssh_config import (
    PROBE_HOST_NAME,
    WorkerHost,
    include_directive,
    write_active_host_config,
)
from ezhpcy.types import PasswordConfig, ProfileConfig
from tests.support.cli import invoke

type Resolver = Callable[[str], dict[str, list[str]] | str | None]


@pytest.fixture(autouse=True)
def config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "ezhpcy.toml"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config.local_file, "config_file", path)
    return path


@pytest.fixture(autouse=True)
def unwrapped_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Rich from wrapping long lines at the test runner's 80 columns."""
    monkeypatch.setattr(doctor_cli.console, "soft_wrap", True)


def resolving_to(hostnames: dict[str, str]) -> Resolver:
    """`ssh -G` as OpenSSH would answer with the probe: the probe's HostName for an
    alias that isn't in `hostnames`."""

    def resolve(alias: str) -> dict[str, list[str]]:
        return {"hostname": [hostnames.get(alias, PROBE_HOST_NAME)]}

    return resolve


@pytest.fixture(autouse=True)
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """An SSH client on PATH that gives every alias the probe's block; returns
    the aliases resolved."""
    monkeypatch.setattr(doctor_cli, "which", lambda _name: "/usr/bin/ssh")
    aliases: list[str] = []
    resolve = resolving_to({})

    def recording(alias: str) -> dict[str, list[str]] | str | None:
        aliases.append(alias)
        return resolve(alias)

    monkeypatch.setattr(doctor_cli, "resolve_alias", recording)
    return aliases


@pytest.fixture
def profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "profile",
        {
            "base": ProfileConfig.model_validate({"scheduler": {"type": "LSF"}}),
            "cpu": ProfileConfig.model_validate(
                {
                    "inherit": "base",
                    "connection": {"host": "login.example.com", "user": "alice"},
                }
            ),
            "gpu": ProfileConfig.model_validate(
                {
                    "inherit": "base",
                    "connection": {"host": "login.example.com", "user": "alice"},
                }
            ),
        },
    )


def worker_host(alias: str) -> WorkerHost:
    return WorkerHost.for_endpoint(alias, user="alice", host="login.example.com")


def test_doctor_passes_when_everything_is_set_up(
    profiles: None, resolved: list[str]
) -> None:
    result = invoke(["doctor"])

    assert result.exit_code == 0, result
    assert "ok    Profile base is valid\n" in result.stdout
    assert "ok    Profile cpu is valid: alice@login.example.com" in result.stdout
    assert "SSH (lets `ssh <alias>` reach running tunnels)" in result.stdout
    assert "ok    'ssh cpu' goes through EzHPCy" in result.stdout
    assert "ok    'ssh gpu' goes through EzHPCy" in result.stdout
    # Only profiles with a user and a host can start a tunnel as they are.
    assert resolved == ["cpu", "gpu"]
    assert "All checks passed." in result.stdout
    # The Include lines are only shown when OpenSSH does not pick the hosts up.
    assert include_directive().splitlines()[-1] not in result.stdout


def test_doctor_probes_with_a_file_it_then_removes(
    profiles: None, monkeypatch: pytest.MonkeyPatch, isolated_runtime_dir: Path
) -> None:
    probe = isolated_runtime_dir / "ssh-config" / "doctor-probe.conf"
    contents: list[str] = []

    def resolve(_alias: str) -> dict[str, list[str]]:
        contents.append(probe.read_text(encoding="utf-8"))
        return {"hostname": [PROBE_HOST_NAME]}

    monkeypatch.setattr(doctor_cli, "resolve_alias", resolve)

    result = invoke(["doctor"])

    assert result.exit_code == 0, result
    assert f"Host cpu\n    HostName {PROBE_HOST_NAME}\n" in contents[0]
    assert "Host base\n" not in contents[0]
    assert not probe.exists()


def test_doctor_accepts_a_running_tunnels_block(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_cli, "resolve_alias", resolving_to({"cpu": "127.0.0.1"}))

    with alias_lock("cpu"):
        result = invoke(["doctor"])

    assert result.exit_code == 0, result
    assert "ok    'ssh cpu' goes through EzHPCy" in result.stdout


def test_doctor_fails_on_a_loopback_block_without_a_tunnel(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hand-written block, say, since no tunnel holds the alias.
    monkeypatch.setattr(doctor_cli, "resolve_alias", resolving_to({"cpu": "127.0.0.1"}))

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert "fail  'ssh cpu' bypasses EzHPCy: OpenSSH resolves it to 127.0.0.1" in (
        result.stdout
    )


def test_doctor_fails_when_openssh_resolves_a_host_elsewhere(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor_cli, "resolve_alias", resolving_to({"cpu": "cpu.example.com"})
    )

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert (
        "fail  'ssh cpu' bypasses EzHPCy: OpenSSH resolves it to cpu.example.com"
        in result.stdout
    )
    assert "ssh gpu" not in result.stdout
    lines = result.stdout.splitlines()
    for line in include_directive().splitlines():
        # Flush-left and on one line, so copying it yields a valid directive.
        assert line in lines


def test_doctor_reports_ssh_refusing_the_configuration(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor_cli, "resolve_alias", lambda _alias: "Bad owner or permissions on x"
    )

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert "fail  'ssh cpu' bypasses EzHPCy: Bad owner or permissions on x" in (
        result.stdout
    )


def test_doctor_checks_the_include_without_profiles(
    monkeypatch: pytest.MonkeyPatch, resolved: list[str]
) -> None:
    monkeypatch.setattr(config, "profile", {})

    result = invoke(["doctor"])

    assert result.exit_code == 0, result
    assert "warn  No profiles are configured" in result.stdout
    assert "Add a '[profile.NAME]' table" in result.stdout
    assert "ok    OpenSSH reads EzHPCy's SSH configuration" in result.stdout
    [alias] = resolved
    assert alias.startswith("ezhpcy-doctor-")


def test_doctor_fails_without_the_include_and_without_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "profile", {})
    monkeypatch.setattr(
        doctor_cli, "resolve_alias", lambda alias: {"hostname": [alias]}
    )

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert "fail  OpenSSH does not read EzHPCy's SSH configuration: OpenSSH " in (
        result.stdout
    )
    assert include_directive().splitlines()[-1] in result.stdout.splitlines()


def test_doctor_removes_stale_blocks(profiles: None) -> None:
    live = write_active_host_config(worker_host("live"), 50022)
    write_active_host_config(worker_host("gone"), 50023)

    with alias_lock("live"):
        result = invoke(["doctor"])

    assert result.exit_code == 0, result
    assert "ok    Removed stale SSH configuration: active-gone.conf" in result.stdout
    assert [path.name for path in live.parent.iterdir()] == ["active-live.conf"]


def test_doctor_reports_a_probe_write_failure(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_path: Path, _content: str) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(ssh_config, "_write_private", fail)

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert "Could not write the SSH probe: read-only file system" in result.stdout


def test_doctor_fails_without_a_configuration_file(
    profiles: None, config_file: Path, isolated_runtime_dir: Path
) -> None:
    config_file.unlink()

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert "fail  Config file not found:" in result.stdout
    assert "ezhpcy config load" in result.stdout
    # Doctor stops at the first failure, before touching the SSH configuration.
    assert "SSH (" not in result.stdout
    assert not (isolated_runtime_dir / "ssh-config").exists()


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (
            ProfileConfig.model_validate({"connection": {"password": "hunter2"}}),
            "profile.broken.connection.password must not be set",
        ),
        (
            ProfileConfig.model_validate(
                {"connection": {"password_file": Path("missing-password.txt")}}
            ),
            "connection.password_file missing-password.txt does not exist",
        ),
    ],
)
def test_doctor_fails_on_invalid_profiles(
    monkeypatch: pytest.MonkeyPatch, profile: ProfileConfig, message: str
) -> None:
    monkeypatch.setattr(config, "profile", {"broken": profile})

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert message in result.stdout


def test_doctor_fails_on_invalid_top_level_password_settings(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config,
        "connection",
        PasswordConfig.model_validate({"password_file": Path("missing.txt")}),
    )

    result = invoke(["doctor"])

    assert result.exit_code == 1
    assert (
        "Top-level connection table is invalid: connection.password_file "
        "missing.txt does not exist" in result.stdout
    )


def test_doctor_ignores_a_password_file_that_a_descriptor_comes_before(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config,
        "connection",
        PasswordConfig.model_validate(
            {"password_fd": 3, "password_file": Path("missing.txt")}
        ),
    )

    result = invoke(["doctor"])

    assert "ok    Top-level connection table is valid" in result.stdout
    assert "does not exist" not in result.stdout


def test_doctor_skips_the_probe_without_ssh(
    profiles: None,
    monkeypatch: pytest.MonkeyPatch,
    resolved: list[str],
    isolated_runtime_dir: Path,
) -> None:
    monkeypatch.setattr(doctor_cli, "which", lambda _name: None)
    write_active_host_config(worker_host("gone"), 50023)

    result = invoke(["doctor"])

    assert result.exit_code == 0, result
    assert "warn  SSH client not found on PATH" in result.stdout
    assert resolved == []
    # Stale blocks are removed all the same.
    assert list((isolated_runtime_dir / "ssh-config").iterdir()) == []
