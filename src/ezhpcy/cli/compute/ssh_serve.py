import ipaddress
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from ezhpcy.cli.compute.common import compute_node_or_fail
from ezhpcy.cli.utils.ssh import absolute_sshd_command
from ezhpcy.config import LocalFileConfig, config
from ezhpcy.constants import (
    OPENSSH_MATCHSPEC,
    SSH_DIRECTORY_NAME,
)

PortArgument = Annotated[
    int,
    typer.Argument(
        min=1024,
        max=65535,
        help="High TCP port on which the worker SSH daemon should listen.",
    ),
]
ListenAddressOption = Annotated[
    str,
    typer.Option(
        "--listen-address",
        "-a",
        help="Address on which the worker SSH daemon should listen.",
    ),
]


def _validate_port(value: int) -> int:
    if not 1024 <= value <= 65535:
        raise typer.BadParameter("must be between 1024 and 65535")
    return value


def _validate_listen_address(value: str) -> str:
    """Reject addresses that cannot be reached from the login node."""
    address = value.strip()
    if not address or address.lower() == "localhost":
        raise typer.BadParameter("must be a non-loopback worker address")

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # Cluster-internal hostnames are valid sshd ListenAddress values.
        return address

    if parsed.is_loopback:
        raise typer.BadParameter("must not be a loopback address")
    return address


def _worker_ssh_paths(
    file_config: LocalFileConfig,
) -> tuple[Path, Path, Path, Path]:
    sshd_config = file_config.config_dir / SSH_DIRECTORY_NAME / "sshd_config"
    sshd_pid = file_config.runtime_dir / f"sshd-{os.getpid()}.pid"
    pixi_home = file_config.data_dir / "pixi_home"
    pixi_cache = file_config.cache_dir / "pixi_cache"
    return sshd_config, sshd_pid, pixi_home, pixi_cache


def _build_sshd_command(
    *,
    pixi_home: Path,
    sshd_config: Path,
    sshd_pid: Path,
    listen_address: str,
    port: int,
    validate_only: bool,
) -> list[str]:
    command = [
        str(pixi_home / "bin" / "pixi"),
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        *absolute_sshd_command([]),
    ]
    if validate_only:
        command.append("-t")
    else:
        command.extend(["-D", "-e"])
    command.extend(
        [
            "-f",
            str(sshd_config),
            "-p",
            str(port),
            "-o",
            f"ListenAddress={listen_address}",
            "-o",
            f"PidFile={sshd_pid}",
        ]
    )
    return command


def ssh_serve_cmd(
    port: PortArgument, listen_address: ListenAddressOption = "0.0.0.0"
) -> None:
    """Validate and run the provisioned worker SSH daemon in the foreground."""
    compute_node_or_fail()
    port = _validate_port(port)
    listen_address = _validate_listen_address(listen_address)

    sshd_config, sshd_pid, pixi_home, pixi_cache = _worker_ssh_paths(config.local_file)
    pixi = pixi_home / "bin" / "pixi"
    if not sshd_config.is_file():
        raise typer.BadParameter(
            f"provisioned SSH configuration was not found at {sshd_config}"
        )
    if not pixi.is_file():
        raise typer.BadParameter(f"private Pixi executable was not found at {pixi}")

    sshd_pid.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(sshd_pid.parent, 0o700)

    environment = os.environ.copy()
    environment["PIXI_HOME"] = str(pixi_home)
    environment["PIXI_CACHE_DIR"] = str(pixi_cache)

    validation_command = _build_sshd_command(
        pixi_home=pixi_home,
        sshd_config=sshd_config,
        sshd_pid=sshd_pid,
        listen_address=listen_address,
        port=port,
        validate_only=True,
    )
    try:
        subprocess.run(validation_command, env=environment, check=True)
    except subprocess.CalledProcessError as error:
        raise typer.Exit(code=error.returncode) from None

    serve_command = _build_sshd_command(
        pixi_home=pixi_home,
        sshd_config=sshd_config,
        sshd_pid=sshd_pid,
        listen_address=listen_address,
        port=port,
        validate_only=False,
    )
    typer.echo(f"Worker SSH endpoint: {listen_address}:{port}", err=True)
    os.execve(str(pixi), serve_command, environment)
