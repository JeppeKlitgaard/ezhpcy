import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from ezhpcy.cli.compute.ssh_serve import (
    _build_sshd_command,
    _validate_listen_address,
    _validate_port,
    ssh_serve_cmd,
)
from ezhpcy.config import LocalFileConfig
from ezhpcy.constants import OPENSSH_MATCHSPEC


def test_build_sshd_command_uses_foreground_mode_and_dynamic_overrides() -> None:
    command = _build_sshd_command(
        pixi_home=Path("/data/ezhpcy/pixi_home"),
        sshd_config=Path("/config/ezhpcy/ssh/sshd_config"),
        sshd_pid=Path("/runtime/ezhpcy/sshd.pid"),
        listen_address="10.0.0.7",
        port=23456,
        validate_only=False,
    )

    assert command == [
        str(Path("/data/ezhpcy/pixi_home/bin/pixi")),
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        "sh",
        "-c",
        'sshd_path="$(command -v sshd)" || exit; case "$sshd_path" in /*) exec "$sshd_path" "$@";; *) echo "sshd must resolve to an absolute path" >&2; exit 1;; esac',
        "sshd",
        "-D",
        "-e",
        "-f",
        str(Path("/config/ezhpcy/ssh/sshd_config")),
        "-p",
        "23456",
        "-o",
        "ListenAddress=10.0.0.7",
        "-o",
        f"PidFile={Path('/runtime/ezhpcy/sshd.pid')}",
    ]


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "localhost", " "])
def test_validate_listen_address_rejects_loopback(address: str) -> None:
    with pytest.raises(typer.BadParameter):
        _validate_listen_address(address)


@pytest.mark.parametrize("address", ["10.0.0.7", "worker-42", "0.0.0.0"])
def test_validate_listen_address_accepts_reachable_or_explicit_wildcard(
    address: str,
) -> None:
    assert _validate_listen_address(address) == address


@pytest.mark.parametrize("port", [0, 1023, 65536])
def test_validate_port_rejects_non_high_or_out_of_range_ports(port: int) -> None:
    with pytest.raises(typer.BadParameter):
        _validate_port(port)


@pytest.mark.parametrize("port", [1024, 23456, 65535])
def test_validate_port_accepts_high_ports(port: int) -> None:
    assert _validate_port(port) == port


def test_ssh_serve_validates_then_replaces_process(tmp_path: Path) -> None:
    file_config = LocalFileConfig(
        cache_dir=tmp_path / "cache" / "ezhpcy",
        config_dir=tmp_path / "config" / "ezhpcy",
        data_dir=tmp_path / "data" / "ezhpcy",
        runtime_dir=tmp_path / "runtime" / "ezhpcy",
        config_file=tmp_path / "config" / "ezhpcy" / "ezhpcy.toml",
    )
    sshd_config = file_config.config_dir / "ssh" / "sshd_config"
    pixi = file_config.data_dir / "pixi_home" / "bin" / "pixi"
    sshd_config.parent.mkdir(parents=True)
    sshd_config.touch()
    pixi.parent.mkdir(parents=True)
    pixi.touch()

    with (
        patch("ezhpcy.cli.compute.ssh_serve.compute_node_or_fail") as guard,
        patch(
            "ezhpcy.cli.compute.ssh_serve.LocalFileConfig",
            return_value=file_config,
        ),
        patch("ezhpcy.cli.compute.ssh_serve.subprocess.run") as run,
        patch("ezhpcy.cli.compute.ssh_serve.os.execve") as execve,
    ):
        ssh_serve_cmd(23456, "worker-42")

    guard.assert_called_once_with()
    validation_command = run.call_args.args[0]
    assert "-t" in validation_command
    assert run.call_args.kwargs["check"] is True
    serve_command = execve.call_args.args[1]
    assert serve_command[7:9] == ["-D", "-e"]
    assert (
        f"PidFile={file_config.runtime_dir / f'sshd-{os.getpid()}.pid'}"
        in serve_command
    )
    assert file_config.runtime_dir.is_dir()
    if os.name == "posix":
        assert file_config.runtime_dir.stat().st_mode & 0o777 == 0o700
    assert execve.call_args.args[2]["PIXI_HOME"] == str(
        file_config.data_dir / "pixi_home"
    )


def test_ssh_serve_defaults_to_all_ipv4_interfaces(tmp_path: Path) -> None:
    file_config = LocalFileConfig(
        cache_dir=tmp_path / "cache" / "ezhpcy",
        config_dir=tmp_path / "config" / "ezhpcy",
        data_dir=tmp_path / "data" / "ezhpcy",
        runtime_dir=tmp_path / "runtime" / "ezhpcy",
        config_file=tmp_path / "config" / "ezhpcy" / "ezhpcy.toml",
    )
    sshd_config = file_config.config_dir / "ssh" / "sshd_config"
    pixi = file_config.data_dir / "pixi_home" / "bin" / "pixi"
    sshd_config.parent.mkdir(parents=True)
    sshd_config.touch()
    pixi.parent.mkdir(parents=True)
    pixi.touch()

    with (
        patch("ezhpcy.cli.compute.ssh_serve.compute_node_or_fail"),
        patch(
            "ezhpcy.cli.compute.ssh_serve.LocalFileConfig",
            return_value=file_config,
        ),
        patch("ezhpcy.cli.compute.ssh_serve.subprocess.run"),
        patch("ezhpcy.cli.compute.ssh_serve.os.execve") as execve,
    ):
        ssh_serve_cmd(23456)

    serve_command = execve.call_args.args[1]
    assert "ListenAddress=0.0.0.0" in serve_command


def test_ssh_serve_stops_when_compute_node_guard_fails() -> None:
    guard_failure = typer.Exit(code=1)

    with (
        patch(
            "ezhpcy.cli.compute.ssh_serve.compute_node_or_fail",
            side_effect=guard_failure,
        ),
        patch("ezhpcy.cli.compute.ssh_serve.subprocess.run") as run,
        patch("ezhpcy.cli.compute.ssh_serve.os.execve") as execve,
        pytest.raises(typer.Exit) as exc_info,
    ):
        ssh_serve_cmd(23456, "worker-42")

    assert exc_info.value is guard_failure
    run.assert_not_called()
    execve.assert_not_called()


def test_ssh_serve_propagates_validation_failure(tmp_path: Path) -> None:
    file_config = LocalFileConfig(
        cache_dir=tmp_path / "cache" / "ezhpcy",
        config_dir=tmp_path / "config" / "ezhpcy",
        data_dir=tmp_path / "data" / "ezhpcy",
        runtime_dir=tmp_path / "runtime" / "ezhpcy",
        config_file=tmp_path / "config" / "ezhpcy" / "ezhpcy.toml",
    )
    sshd_config = file_config.config_dir / "ssh" / "sshd_config"
    pixi = file_config.data_dir / "pixi_home" / "bin" / "pixi"
    sshd_config.parent.mkdir(parents=True)
    sshd_config.touch()
    pixi.parent.mkdir(parents=True)
    pixi.touch()
    failure = subprocess.CalledProcessError(1, [str(pixi)])

    with (
        patch("ezhpcy.cli.compute.ssh_serve.compute_node_or_fail"),
        patch(
            "ezhpcy.cli.compute.ssh_serve.LocalFileConfig",
            return_value=file_config,
        ),
        patch("ezhpcy.cli.compute.ssh_serve.subprocess.run", side_effect=failure),
        patch("ezhpcy.cli.compute.ssh_serve.os.execve") as execve,
        pytest.raises(typer.Exit) as exc_info,
    ):
        ssh_serve_cmd(23456, "worker-42")

    assert exc_info.value.exit_code == failure.returncode
    execve.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "expected_output"),
    [
        (["version"], "0.1.0"),
        (["compute", "ssh-serve", "--help"], "Start an SSH server"),
    ],
)
def test_cluster_commands_do_not_load_workstation_config(
    tmp_path: Path, arguments: list[str], expected_output: str
) -> None:
    invalid_config = tmp_path / "incompatible.toml"
    invalid_config.write_text("this is not valid TOML = [", encoding="utf-8")
    environment = {
        **os.environ,
        "EZHPCY_CONFIG_FILE": str(invalid_config),
    }

    result = subprocess.run(
        [sys.executable, "-m", "ezhpcy.cli.entry", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected_output in result.stdout
