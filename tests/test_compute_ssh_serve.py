import subprocess
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
        listen_address="10.0.0.7",
        port=23456,
        validate_only=False,
    )

    assert command == [
        str(Path("/data/ezhpcy/pixi_home/bin/pixi")),
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        "sshd",
        "-D",
        "-e",
        "-f",
        str(Path("/config/ezhpcy/ssh/sshd_config")),
        "-p",
        "23456",
        "-o",
        "ListenAddress=10.0.0.7",
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
        patch("ezhpcy.cli.compute.ssh_serve.config.local_file", file_config),
        patch("ezhpcy.cli.compute.ssh_serve.subprocess.run") as run,
        patch("ezhpcy.cli.compute.ssh_serve.os.execve") as execve,
    ):
        ssh_serve_cmd(23456, "worker-42")

    guard.assert_called_once_with()
    validation_command = run.call_args.args[0]
    assert "-t" in validation_command
    assert run.call_args.kwargs["check"] is True
    serve_command = execve.call_args.args[1]
    assert serve_command[4:6] == ["-D", "-e"]
    assert execve.call_args.args[2]["PIXI_HOME"] == str(
        file_config.data_dir / "pixi_home"
    )


def test_ssh_serve_defaults_to_all_ipv4_interfaces(tmp_path: Path) -> None:
    file_config = LocalFileConfig(
        cache_dir=tmp_path / "cache" / "ezhpcy",
        config_dir=tmp_path / "config" / "ezhpcy",
        data_dir=tmp_path / "data" / "ezhpcy",
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
        patch("ezhpcy.cli.compute.ssh_serve.config.local_file", file_config),
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
        patch("ezhpcy.cli.compute.ssh_serve.config.local_file", file_config),
        patch("ezhpcy.cli.compute.ssh_serve.subprocess.run", side_effect=failure),
        patch("ezhpcy.cli.compute.ssh_serve.os.execve") as execve,
        pytest.raises(typer.Exit) as exc_info,
    ):
        ssh_serve_cmd(23456, "worker-42")

    assert exc_info.value.exit_code == failure.returncode
    execve.assert_not_called()
