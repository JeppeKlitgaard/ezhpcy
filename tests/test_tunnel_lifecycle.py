from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app
from ezhpcy.cli.tunnel import common
from ezhpcy.cli.tunnel.provision import (
    WORKER_HOST_ALIAS,
    ProvisioningError,
    _pin_worker_host_key,
    _render_sshd_config,
    provision_pixi,
    validate_worker_infrastructure,
)
from ezhpcy.cli.tunnel.prune import prune_stale_payloads
from ezhpcy.config import LocalConfig
from ezhpcy.constants import (
    PIXI_INSTALLER_URL,
    PIXI_VERSION,
)
from ezhpcy.types import ProfileConfig, RemoteState
from ezhpcy.worker_payload import render_worker_payload, worker_payload_path


class StubSFTP:
    def __init__(self) -> None:
        self.files: dict[str, str | bytes] = {}
        self.chmods: list[tuple[str, int]] = []
        self.puts: list[tuple[str, str]] = []
        self.directory_entries: list[str] = []

    def mkdir(self, *_args, **_kwargs) -> None:
        pass

    def write_text(self, path, content: str) -> int:
        self.files[str(path)] = content
        return len(content)

    def chmod(self, path: str, mode: int) -> None:
        self.chmods.append((path, mode))

    def put(self, source: str, destination: str) -> None:
        self.puts.append((source, destination))
        self.files[destination] = b"uploaded"

    def stat(self, path: str) -> SimpleNamespace:
        if path not in self.files:
            raise FileNotFoundError(path)
        return SimpleNamespace()

    def read_text(self, path) -> str:
        if str(path).endswith(".pub"):
            return "ssh-ed25519 REMOTE worker-host\n"
        value = self.files[str(path)]
        return value if isinstance(value, str) else value.decode()

    def read_bytes(self, path) -> bytes:
        value = self.files[str(path)]
        return value if isinstance(value, bytes) else value.encode()

    def listdir_attr(self, _path: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(filename=name) for name in self.directory_entries]


class StubSSH:
    def __init__(self) -> None:
        self.sftp = StubSFTP()
        self.commands: list[list[str]] = []
        self.pixi_commands: list[list[str]] = []
        self.remote_state = RemoteState(
            cache_dir=PurePosixPath("/home/alice/.cache"),
        )

    def interactive_connect(self) -> None:
        pass

    def get_remote_state(self) -> RemoteState:
        return self.remote_state

    @contextmanager
    def sftp_client(self):
        yield self.sftp

    def run(self, args: list[str]) -> str:
        self.commands.append(args)
        return ""

    def run_pixi(self, args: list[str], **_kwargs) -> str:
        self.pixi_commands.append(args)
        if "ssh-keygen" in args:
            key_path = args[args.index("-f") + 1]
            self.sftp.files[key_path] = b"private key"
            self.sftp.files[f"{key_path}.pub"] = b"public key"
        return ""


def configured_client(tmp_path: Path) -> LocalConfig:
    return LocalConfig.from_mapping(
        {
            "local_file": {
                "config_dir": tmp_path / "config",
                "cache_dir": tmp_path / "cache",
                "data_dir": tmp_path / "data",
                "runtime_dir": tmp_path / "runtime",
            },
            "default_profile": "default",
            "profile": {
                "default": ProfileConfig(
                    host="login.example.com", user="alice", scheduler="LSF"
                )
            },
        }
    )


def test_provision_help_describes_idempotent_provisioning() -> None:
    result = CliRunner().invoke(app, ["tunnel", "provision", "--help"])

    assert result.exit_code == 0
    assert "Provision ezhpcy worker infrastructure" in result.stdout

    prune_help = CliRunner().invoke(app, ["tunnel", "prune", "--help"])
    assert prune_help.exit_code == 0
    assert "--all" in prune_help.stdout
    assert "-a" in prune_help.stdout


def test_provision_is_repeatable_and_never_invokes_remote_python(
    tmp_path: Path,
) -> None:
    ssh = StubSSH()
    config = configured_client(tmp_path)
    private_key = tmp_path / "worker_client_ed25519"
    public_key = private_key.with_suffix(".pub")
    public_key.write_text("ssh-ed25519 LOCAL\n", encoding="utf-8")
    (config.local_file.config_dir / "ssh").mkdir(parents=True)
    payload_path = PurePosixPath("/home/alice/.cache/ezhpcy/payloads/abc123/ssh-serve")

    with (
        patch.object(common, "get_config", return_value=config),
        patch("ezhpcy.cli.tunnel.provision.get_config", return_value=config),
        patch("ezhpcy.cli.tunnel.provision.InteractiveSSHClient", return_value=ssh),
        patch(
            "ezhpcy.cli.tunnel.provision._ensure_local_client_key",
            return_value=(private_key, public_key),
        ),
        patch(
            "ezhpcy.cli.tunnel.provision.ensure_worker_payload",
            return_value=payload_path,
        ) as ensure_payload,
    ):
        first = CliRunner().invoke(app, ["tunnel", "provision", "--yes"])
        second = CliRunner().invoke(app, ["tunnel", "provision", "--yes"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert len(ssh.commands) == 2
    assert all(command[:2] == ["bash", "-c"] for command in ssh.commands)
    for _bash, _option, command in ssh.commands:
        assert PIXI_INSTALLER_URL in command
        assert f"PIXI_VERSION={PIXI_VERSION}" in command
        assert f"/ezhpcy/pixi/{PIXI_VERSION}/bin/pixi" in command
        assert "curl --fail --location --show-error --silent" in command
        assert "provision.sh" not in command
    assert not any(command[0] == "ezhpcy" for command in ssh.commands)
    assert not any(
        "uv" in argument for command in ssh.pixi_commands for argument in command
    )
    assert sum("ssh-keygen" in command for command in ssh.pixi_commands) == 1
    assert ensure_payload.call_count == 2
    assert "/home/alice/.cache/ezhpcy/provision.sh" not in ssh.sftp.files


def test_provision_pixi_installs_the_pinned_version_in_a_versioned_cache() -> None:
    ssh = StubSSH()

    provision_pixi(ssh, ssh.remote_state)  # type: ignore[arg-type]

    assert len(ssh.commands) == 1
    command = ssh.commands[0]
    assert command[:2] == ["bash", "-c"]
    inline_script = command[2]
    expected_home = f"/home/alice/.cache/ezhpcy/pixi/{PIXI_VERSION}"
    assert f"if [ ! -x {expected_home}/bin/pixi ]" in inline_script
    assert f"PIXI_HOME={expected_home}" in inline_script
    assert f"PIXI_VERSION={PIXI_VERSION}" in inline_script
    assert PIXI_INSTALLER_URL in inline_script


def test_default_prune_removes_only_stale_hashed_payloads() -> None:
    ssh = StubSSH()
    current = worker_payload_path(ssh.remote_state).parent.name
    stale_hash = "a" * 64 if current != "a" * 64 else "b" * 64
    ssh.sftp.directory_entries = [current, stale_hash, "keep-me"]

    removed = prune_stale_payloads(ssh, ssh.remote_state)  # type: ignore[arg-type]

    stale_path = PurePosixPath(f"/home/alice/.cache/ezhpcy/payloads/{stale_hash}")
    assert removed == (stale_path,)
    assert ssh.commands == [["rm", "-rf", "--", str(stale_path)]]


def test_validate_worker_infrastructure_is_read_only() -> None:
    ssh = StubSSH()
    remote_ssh = PurePosixPath("/home/alice/.cache/ezhpcy/ssh")
    required = (
        PurePosixPath(f"/home/alice/.cache/ezhpcy/pixi/{PIXI_VERSION}/bin/pixi"),
        remote_ssh / "authorized_keys",
        remote_ssh / "ssh_host_ed25519_key",
        remote_ssh / "ssh_host_ed25519_key.pub",
        remote_ssh / "sshd_config",
    )
    for path in required:
        ssh.sftp.files[str(path)] = b"present"
    payload_path = worker_payload_path(ssh.remote_state)
    ssh.sftp.files[str(payload_path)] = render_worker_payload()

    resolved = validate_worker_infrastructure(  # type: ignore[arg-type]
        ssh, ssh.remote_state
    )

    assert resolved == payload_path
    assert ssh.commands == []


def test_validate_worker_infrastructure_reports_missing_files() -> None:
    ssh = StubSSH()

    with pytest.raises(ProvisioningError, match="tunnel provision") as exc_info:
        validate_worker_infrastructure(ssh, ssh.remote_state)  # type: ignore[arg-type]

    assert f"/ezhpcy/pixi/{PIXI_VERSION}/bin/pixi" in str(exc_info.value)


def test_prune_all_removes_the_package_cache_directory(tmp_path: Path) -> None:
    ssh = StubSSH()
    config = configured_client(tmp_path)

    with (
        patch.object(common, "get_config", return_value=config),
        patch("ezhpcy.cli.tunnel.prune.InteractiveSSHClient", return_value=ssh),
    ):
        result = CliRunner().invoke(app, ["tunnel", "prune", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    assert ssh.commands == [["rm", "-rf", "--", "/home/alice/.cache/ezhpcy"]]


def test_install_and_uninstall_commands_have_been_removed() -> None:
    for command in ("install", "uninstall"):
        result = CliRunner().invoke(app, ["tunnel", command, "--help"])
        assert result.exit_code == 2


def test_render_sshd_config_replaces_remote_values() -> None:
    rendered = _render_sshd_config(
        "AllowUsers {{ remote_username }}\nHostKey {{ remote_config_dir }}/host\n",
        remote_username="alice",
        remote_config_dir=PurePosixPath("/home/alice/.config/ezhpcy/ssh"),
    )

    assert rendered == ("AllowUsers alice\nHostKey /home/alice/.config/ezhpcy/ssh/host")


def test_pin_worker_host_key_preserves_unrelated_entries(tmp_path: Path) -> None:
    known_hosts = tmp_path / "worker_known_hosts"
    known_hosts.write_text(
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 OLD\n",
        encoding="utf-8",
    )

    _pin_worker_host_key("ssh-ed25519 NEW remote-comment\n", known_hosts)

    assert known_hosts.read_text(encoding="utf-8") == (
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 NEW\n"
    )
