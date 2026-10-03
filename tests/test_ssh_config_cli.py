from pathlib import Path

import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app, ssh_config as ssh_config_cli
from ezhpcy.config import config
from ezhpcy.tunnel.ssh_config import WorkerHost, include_directive
from ezhpcy.types import ProfileConfig


@pytest.fixture
def profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "profile",
        {
            "base": ProfileConfig(scheduler="LSF"),
            "cpu": ProfileConfig(host="login.example.com", user="alice"),
            "gpu": ProfileConfig(host="login.example.com", user="alice"),
        },
    )


def test_ssh_config_writes_profiles_and_prints_copyable_include_lines(
    profiles: None, isolated_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_config_cli, "check_host_resolution", lambda _host: None)

    result = CliRunner().invoke(app, ["ssh-config"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    for line in include_directive().splitlines():
        # Flush-left and on one line, so copying it yields a valid directive.
        assert line in lines
    profiles_conf = isolated_runtime_dir / "ssh-config" / "profiles.conf"
    content = profiles_conf.read_text(encoding="utf-8")
    assert "Host cpu\n" in content
    assert "Host gpu\n" in content
    assert "Host base\n" not in content


def test_ssh_config_reports_each_profile_alias(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def check(host: WorkerHost) -> str | None:
        return "shadowed by ~/.ssh/config" if host.alias == "gpu" else None

    monkeypatch.setattr(ssh_config_cli, "check_host_resolution", check)

    result = CliRunner().invoke(app, ["ssh-config"])

    assert result.exit_code == 0, result.output
    assert "ok    cpu" in result.stdout
    assert "fail  gpu: shadowed by ~/.ssh/config" in result.stdout


def test_ssh_config_without_complete_profiles_still_prints_the_include(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "profile", {})

    result = CliRunner().invoke(app, ["ssh-config"])

    assert result.exit_code == 0, result.output
    assert include_directive().splitlines()[-1] in result.stdout.splitlines()
    assert "No profile defines both a user and a host" in result.stdout


def test_ssh_config_write_failure_is_reported(
    profiles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_hosts: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(ssh_config_cli, "write_profiles_config", fail)

    result = CliRunner().invoke(app, ["ssh-config"])

    assert result.exit_code == 1
    assert "Could not write SSH configuration" in result.stdout
    assert "read-only file system" in result.stdout
