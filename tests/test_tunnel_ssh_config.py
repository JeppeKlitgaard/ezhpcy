import sys
from pathlib import Path

from typer.testing import CliRunner

from ezhpcy.cli import app, ssh_config
from ezhpcy.cli.ssh_config import render_worker_ssh_config
from ezhpcy.types import ProfileConfig


def test_render_worker_ssh_config_contains_complete_strict_proxy_configuration(
    tmp_path: Path,
) -> None:
    rendered = render_worker_ssh_config(
        user="alice",
        profile_name="gpu",
        ssh_dir=tmp_path / "config with spaces" / "ssh",
        python_executable=tmp_path / "runtime with spaces" / "python.exe",
    )

    assert rendered == (
        "Host gpu\n"
        "    HostName gpu\n"
        "    User alice\n"
        f'    IdentityFile "{(tmp_path / "config with spaces" / "ssh" / "worker_client_ed25519").as_posix()}"\n'
        "    IdentitiesOnly yes\n"
        f'    UserKnownHostsFile "{(tmp_path / "config with spaces" / "ssh" / "worker_known_hosts").as_posix()}"\n'
        "    HostKeyAlias ezhpcy-worker\n"
        "    StrictHostKeyChecking yes\n"
        f'    ProxyCommand "{(tmp_path / "runtime with spaces" / "python.exe").as_posix()}" -m ezhpcy.cli proxy gpu\n'
    )


def test_ssh_config_command_is_available_and_uses_current_python(monkeypatch) -> None:
    monkeypatch.setattr(
        "ezhpcy.utils.machineid.hashed_id", lambda _app_id: "machine-id"
    )
    monkeypatch.setattr(
        ssh_config.config,
        "profile",
        {"default": ProfileConfig(host="login.example.com", user="alice")},
    )
    result = CliRunner().invoke(
        app,
        ["ssh-config", "default"],
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("Host default\n")
    assert "    User alice\n" in result.stdout
    assert f'    ProxyCommand "{Path(sys.executable).as_posix()}"' in result.stdout
    assert "proxy default" in result.stdout
    expected_ssh_dir = (
        ssh_config.config.local_file.config_dir
        / "ssh"
        / "machine-id"
        / "alice@login.example.com"
    ).resolve()
    assert f'    IdentityFile "{expected_ssh_dir.as_posix()}/' in result.stdout


def test_ssh_config_command_accepts_an_alias_override(monkeypatch) -> None:
    monkeypatch.setattr(
        "ezhpcy.utils.machineid.hashed_id", lambda _app_id: "machine-id"
    )
    monkeypatch.setattr(
        ssh_config.config,
        "profile",
        {"gpu": ProfileConfig(host="login.example.com", user="alice")},
    )

    result = CliRunner().invoke(
        app,
        ["ssh-config", "gpu", "--alias", "cluster-worker"],
    )

    assert result.exit_code == 0
    assert result.stdout.startswith("Host cluster-worker\n")
    assert "proxy gpu" in result.stdout


def test_ssh_config_command_requires_a_profile_argument() -> None:
    result = CliRunner().invoke(app, ["ssh-config"])

    assert result.exit_code == 2
    assert "Missing argument 'PROFILE'" in result.stderr
