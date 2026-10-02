from pathlib import Path

import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app, doctor as doctor_cli
from ezhpcy.config import config
from ezhpcy.tunnel.ssh_config import WorkerHost, include_directive
from ezhpcy.types import ProfileConfig


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


@pytest.fixture(autouse=True)
def ssh_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_cli, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(doctor_cli, "check_host_resolution", lambda _host: None)


@pytest.fixture
def profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "profile",
        {
            "base": ProfileConfig(scheduler="LSF"),
            "cpu": ProfileConfig(
                inherit="base", host="login.example.com", user="alice"
            ),
            "gpu": ProfileConfig(
                inherit="base", host="login.example.com", user="alice"
            ),
        },
    )


def test_doctor_passes_when_everything_is_set_up(profiles: None) -> None:

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "ok    Profile base is valid\n" in result.stdout
    assert "ok    Profile cpu is valid: alice@login.example.com" in result.stdout
    assert "ok    `ssh cpu` goes through EzHPCy" in result.stdout
    assert "All checks passed." in result.stdout


def test_doctor_fails_without_a_configuration_file(
    profiles: None, config_file: Path, isolated_runtime_dir: Path
) -> None:
    config_file.unlink()

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "fail  Config file not found:" in result.stdout
    assert "ezhpcy config load" in result.stdout
    # Doctor stops at the first failure, before touching the SSH configuration.
    assert "SSH (" not in result.stdout
    assert not (isolated_runtime_dir / "ssh-config").exists()


def test_doctor_warns_without_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "profile", {})

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "warn  No profiles are configured" in result.stdout
    assert "so the SSH setup cannot be checked" in result.stdout
    assert include_directive().splitlines()[-1] in result.stdout.splitlines()


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (ProfileConfig(password="hunter2"), "must not set password"),
        (
            ProfileConfig(password_file=Path("missing-password.txt")),
            "password_file missing-password.txt does not exist",
        ),
    ],
)
def test_doctor_fails_on_invalid_profiles(
    monkeypatch: pytest.MonkeyPatch, profile: ProfileConfig, message: str
) -> None:
    monkeypatch.setattr(config, "profile", {"broken": profile})

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert message in result.stdout


def test_doctor_regenerates_the_profile_ssh_hosts(
    profiles: None, isolated_runtime_dir: Path
) -> None:
    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    profiles_conf = isolated_runtime_dir / "ssh-config" / "profiles.conf"
    content = profiles_conf.read_text(encoding="utf-8")
    assert "Host cpu\n" in content
    assert "Host base\n" not in content
    # The Include lines are only shown when OpenSSH does not pick the hosts up.
    assert include_directive().splitlines()[-1] not in result.stdout


def test_doctor_reports_ssh_config_write_failure(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_hosts: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(doctor_cli, "write_profiles_config", fail)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "Could not write SSH hosts: read-only file system" in (result.stdout)


def test_doctor_fails_when_openssh_resolves_a_host_elsewhere(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def check(host: WorkerHost) -> str | None:
        return "shadowed by ~/.ssh/config" if host.alias == "cpu" else None

    monkeypatch.setattr(doctor_cli, "check_host_resolution", check)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "fail  `ssh cpu` bypasses EzHPCy: shadowed by ~/.ssh/config" in result.stdout
    assert "`ssh gpu`" not in result.stdout
    lines = result.stdout.splitlines()
    for line in include_directive().splitlines():
        # Flush-left and on one line, so copying it yields a valid directive.
        assert line in lines


def test_doctor_skips_host_resolution_without_ssh(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(_host: WorkerHost) -> str | None:
        raise AssertionError("host resolution should be skipped")

    monkeypatch.setattr(doctor_cli, "which", lambda _name: None)
    monkeypatch.setattr(doctor_cli, "check_host_resolution", unexpected)

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "warn  SSH client not found on PATH" in result.stdout
