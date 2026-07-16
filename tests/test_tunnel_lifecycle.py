from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import paramiko
import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app, common
from ezhpcy.cli.provision import (
    WORKER_HOST_ALIAS,
    ProvisioningError,
    _ensure_local_ssh_keys,
    _pin_worker_host_key,
    _restrict_private_key_permissions,
    provision_openssh,
    provision_pixi,
    provision_sshd_files,
    validate_worker_infrastructure,
)
from ezhpcy.cli.prune import (
    prune_stale_installations,
    prune_stale_pixi_data,
)
from ezhpcy.cli.utils.ssh import absolute_sshd_command, sshd_config_arguments
from ezhpcy.config import Config
from ezhpcy.constants import (
    EZHPCY_VERSION,
    OPENSSH_MATCHSPEC,
    PIXI_INSTALLER_URL,
    PIXI_VERSION,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_KEY_NAME,
)
from ezhpcy.types import ProfileConfig, RemoteState


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
        return ""


def configured_client(tmp_path: Path) -> Config:
    return Config.from_mapping(
        {
            "local_file": {
                "config_dir": tmp_path / "config",
                "cache_dir": tmp_path / "cache",
                "data_dir": tmp_path / "data",
                "runtime_dir": tmp_path / "runtime",
            },
            "profile": {
                "base": ProfileConfig(
                    host="login.example.com", user="alice", scheduler="LSF"
                )
            },
        }
    )


def test_provision_help_describes_idempotent_provisioning() -> None:
    result = CliRunner().invoke(app, ["provision", "--help"])

    assert result.exit_code == 0
    assert "Provision EzHPCy worker infrastructure" in result.stdout

    prune_help = CliRunner().invoke(app, ["prune", "--help"])
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
    host_private_key = tmp_path / "ssh_host_ed25519_key"
    host_public_key = host_private_key.with_suffix(".pub")
    host_public_key.write_text("ssh-ed25519 LOCAL-HOST\n", encoding="utf-8")
    machine_ssh_dir = (
        config.local_file.config_dir / "ssh" / "machine-id" / "alice@login.example.com"
    )
    machine_ssh_dir.mkdir(parents=True)
    with (
        patch("ezhpcy.utils.machineid.hashed_id", return_value="machine-id"),
        patch.object(common, "config", config),
        patch("ezhpcy.cli.provision.config", config),
        patch("ezhpcy.cli.provision.InteractiveSSHClient", return_value=ssh),
        patch(
            "ezhpcy.cli.provision.provision_sshd_files",
            wraps=provision_sshd_files,
        ) as provision_files,
        patch(
            "ezhpcy.cli.provision._ensure_local_ssh_keys",
            return_value=(
                private_key,
                public_key,
                host_private_key,
                host_public_key,
            ),
        ),
    ):
        first = CliRunner().invoke(app, ["provision", "base", "--yes"])
        second = CliRunner().invoke(app, ["provision", "base", "--yes"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert provision_files.call_count == 2
    assert len(ssh.commands) == 2
    assert all(command[:2] == ["bash", "-c"] for command in ssh.commands)
    for _bash, _option, command in ssh.commands:
        assert PIXI_INSTALLER_URL in command
        assert f"PIXI_VERSION={PIXI_VERSION}" in command
        assert f"/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi" in command
        assert "curl --fail --location --show-error --silent" in command
        assert "provision.sh" not in command
    assert not any(command[0] == "ezhpcy" for command in ssh.commands)
    assert not any(
        "uv" in argument for command in ssh.pixi_commands for argument in command
    )
    assert not any("ssh-keygen" in command for command in ssh.pixi_commands)
    remote_ssh = (
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/ssh/machine-id/"
        "alice@login.example.com"
    )
    assert (
        ssh.sftp.puts
        == [
            (str(host_private_key), f"{remote_ssh}/ssh_host_ed25519_key"),
            (str(host_public_key), f"{remote_ssh}/ssh_host_ed25519_key.pub"),
        ]
        * 2
    )
    assert (machine_ssh_dir / "worker_known_hosts").read_text(
        encoding="utf-8"
    ) == f"{WORKER_HOST_ALIAS} ssh-ed25519 LOCAL-HOST\n"
    assert f"{remote_ssh}/authorized_keys" not in ssh.sftp.files
    assert f"{remote_ssh}/sshd_config" not in ssh.sftp.files
    validation_commands = [command for command in ssh.pixi_commands if "-t" in command]
    assert len(validation_commands) == 2
    for command in validation_commands:
        assert "-f" in command
        assert "/dev/null" in command
        assert f"HostKey={remote_ssh}/ssh_host_ed25519_key" in command
        assert "AuthorizedKeysFile=none" in command
        assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 LOCAL" in command
    assert "/home/alice/.cache/ezhpcy/provision.sh" not in ssh.sftp.files


def test_local_worker_keys_are_generated_and_reused(
    tmp_path: Path, monkeypatch
) -> None:
    ssh_dir = tmp_path / "config" / "ssh" / "machine-id" / "alice@login.example.com"
    generated_keys = 0

    def count_generated_key():
        nonlocal generated_keys
        generated_keys += 1
        return original_generate()

    from cryptography.hazmat.primitives.asymmetric import ed25519

    original_generate = ed25519.Ed25519PrivateKey.generate
    monkeypatch.setattr(ed25519.Ed25519PrivateKey, "generate", count_generated_key)

    first = _ensure_local_ssh_keys(ssh_dir)
    initial_contents = [path.read_bytes() for path in first]
    second = _ensure_local_ssh_keys(ssh_dir)

    assert first == second
    assert [path.name for path in first] == [
        WORKER_CLIENT_KEY_NAME,
        f"{WORKER_CLIENT_KEY_NAME}.pub",
        WORKER_HOST_KEY_NAME,
        f"{WORKER_HOST_KEY_NAME}.pub",
    ]
    assert all(path.parent == ssh_dir and path.is_file() for path in first)
    assert generated_keys == 2
    assert [path.read_bytes() for path in second] == initial_contents
    assert [
        path.read_text(encoding="ascii").split(maxsplit=2)[2].strip()
        for path in (first[1], first[3])
    ] == [
        "ezhpcy worker client",
        "ezhpcy worker host",
    ]
    for private_key, public_key in ((first[0], first[1]), (first[2], first[3])):
        loaded_key = paramiko.Ed25519Key.from_private_key_file(str(private_key))
        public_fields = public_key.read_text(encoding="ascii").split()
        assert public_fields[:2] == [loaded_key.get_name(), loaded_key.get_base64()]


def test_windows_private_key_acl_grants_only_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key = tmp_path / "worker_client_ed25519"
    private_key.touch()
    completed = SimpleNamespace(stdout='"MACHINE\\alice","S-1-5-21-1001"\n')

    monkeypatch.setattr("ezhpcy.cli.provision.os.name", "nt")
    with patch("ezhpcy.cli.provision.subprocess.run", return_value=completed) as run:
        _restrict_private_key_permissions(private_key)

    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == [
        "whoami",
        "/user",
        "/fo",
        "csv",
        "/nh",
    ]
    assert run.call_args_list[1].args[0] == [
        "icacls",
        str(private_key),
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-21-1001:(F)",
    ]


def test_local_worker_host_keys_differ_between_login_endpoints(
    tmp_path: Path,
) -> None:
    machine_ssh_dir = tmp_path / "config" / "ssh" / "machine-id"
    login1_keys = _ensure_local_ssh_keys(machine_ssh_dir / "alice@login1.hpc.dtu.dk")
    login2_keys = _ensure_local_ssh_keys(machine_ssh_dir / "alice@login2.hpc.dtu.dk")

    assert login1_keys[3].read_text(encoding="ascii") != login2_keys[3].read_text(
        encoding="ascii"
    )


def test_provision_pixi_installs_the_pinned_version_in_a_versioned_cache() -> None:
    ssh = StubSSH()

    provision_pixi(ssh, ssh.remote_state)  # type: ignore[arg-type]

    assert len(ssh.commands) == 1
    command = ssh.commands[0]
    assert command[:2] == ["bash", "-c"]
    inline_script = command[2]
    expected_home = f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}"
    assert f"if [ ! -x {expected_home}/bin/pixi ]" in inline_script
    assert f"PIXI_HOME={expected_home}" in inline_script
    assert f"PIXI_VERSION={PIXI_VERSION}" in inline_script
    assert PIXI_INSTALLER_URL in inline_script


def test_provision_openssh_reuses_absolute_sshd_command() -> None:
    ssh = StubSSH()

    provision_openssh(ssh, ssh.remote_state)  # type: ignore[arg-type]

    assert ssh.pixi_commands == [
        [
            "exec",
            f"--spec={OPENSSH_MATCHSPEC}",
            *absolute_sshd_command(["-V"]),
        ]
    ]


def test_default_prune_removes_stale_ezhpcy_installations() -> None:
    ssh = StubSSH()
    ssh.sftp.directory_entries = [EZHPCY_VERSION, "0.0.9", "legacy"]

    removed = prune_stale_installations(  # type: ignore[arg-type]
        ssh, ssh.remote_state
    )

    remote_root = PurePosixPath("/home/alice/.cache/ezhpcy")
    assert removed == (remote_root / "0.0.9", remote_root / "legacy")
    assert ssh.commands == [
        [
            "rm",
            "-rf",
            "--",
            str(remote_root / "0.0.9"),
            str(remote_root / "legacy"),
        ]
    ]


def test_default_prune_removes_stale_pixi_homes_and_caches() -> None:
    ssh = StubSSH()
    ssh.sftp.directory_entries = [PIXI_VERSION, "0.72.0", "keep me"]

    removed = prune_stale_pixi_data(  # type: ignore[arg-type]
        ssh, ssh.remote_state
    )

    package_root = PurePosixPath(f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}")
    stale_home = package_root / "pixi" / "0.72.0"
    stale_cache = package_root / "pixi_cache" / "0.72.0"
    assert removed == (stale_home, stale_cache)
    assert ssh.commands == [["rm", "-rf", "--", str(stale_home), str(stale_cache)]]


def test_validate_worker_infrastructure_is_read_only() -> None:
    ssh = StubSSH()
    remote_ssh = PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/ssh/machine-id/"
        "alice@login.example.com"
    )
    required = (
        PurePosixPath(
            f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi"
        ),
        remote_ssh / "ssh_host_ed25519_key",
        remote_ssh / "ssh_host_ed25519_key.pub",
    )
    for path in required:
        ssh.sftp.files[str(path)] = b"present"
    result = validate_worker_infrastructure(  # type: ignore[arg-type]
        ssh,
        ssh.remote_state,
        remote_username="alice",
        remote_host="login.example.com",
        machine_id="machine-id",
    )

    assert result is None
    assert ssh.commands == []
    assert ssh.pixi_commands == []


def test_validate_worker_infrastructure_reports_missing_files() -> None:
    ssh = StubSSH()

    with pytest.raises(ProvisioningError, match="ezhpcy provision") as exc_info:
        validate_worker_infrastructure(  # type: ignore[arg-type]
            ssh,
            ssh.remote_state,
            remote_username="alice",
            remote_host="login.example.com",
            machine_id="machine-id",
        )

    assert f"/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi" in str(
        exc_info.value
    )
    assert (
        f"/ezhpcy/{EZHPCY_VERSION}/ssh/machine-id/"
        "alice@login.example.com/ssh_host_ed25519_key"
    ) in str(exc_info.value)


def test_prune_all_removes_the_package_cache_directory(tmp_path: Path) -> None:
    ssh = StubSSH()
    config = configured_client(tmp_path)

    with (
        patch.object(common, "config", config),
        patch("ezhpcy.cli.prune.InteractiveSSHClient", return_value=ssh),
    ):
        result = CliRunner().invoke(app, ["prune", "base", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    assert ssh.commands == [["rm", "-rf", "--", "/home/alice/.cache/ezhpcy"]]


def test_tunnel_and_legacy_lifecycle_commands_have_been_removed() -> None:
    for command in ("tunnel", "t", "install", "uninstall"):
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 2


def test_sshd_config_is_expressed_as_cli_arguments() -> None:
    arguments = sshd_config_arguments(
        host_key=PurePosixPath("/home/alice/.config/ezhpcy/ssh/host"),
        remote_username="alice",
        authorized_key=("ssh-ed25519", "PUBLICKEY"),
    )

    assert arguments[:2] == ["-f", "/dev/null"]
    settings = arguments[3::2]
    assert "HostKey=/home/alice/.config/ezhpcy/ssh/host" in settings
    assert "AuthorizedKeysFile=none" in settings
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 PUBLICKEY" in settings
    assert "AuthorizedKeysCommandUser=alice" in settings
    assert "AllowUsers=alice" in settings
    assert "Subsystem=sftp internal-sftp" in settings


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
