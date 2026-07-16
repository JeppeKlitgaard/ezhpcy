from binascii import hexlify
from pathlib import Path, PurePosixPath

import paramiko
from paramiko.common import DEBUG
from rich.prompt import Confirm, Prompt

from ezhpcy import console
from ezhpcy.config import ConnectionInfo
from ezhpcy.ssh import SSHClient

_EXEC_ABSOLUTE_SSHD = (
    'sshd_path="$(command -v sshd)" || exit; '
    'case "$sshd_path" in /*) exec "$sshd_path" "$@";; '
    '*) echo "sshd must resolve to an absolute path" >&2; exit 1;; esac'
)
SSHD_PORT_MARKER = "ezhpcy: worker sshd selected port "
_EXEC_RETRYING_SSHD = (
    'sshd_path="$(command -v sshd)" || exit; '
    'case "$sshd_path" in /*) ;; '
    '*) echo "sshd must resolve to an absolute path" >&2; exit 1;; esac; '
    'port_count="$1"; shift; pid=; '
    'trap \'[ -z "$pid" ] || kill "$pid" 2>/dev/null; exit 143\' '
    "HUP INT TERM; "
    'while [ "$port_count" -gt 0 ]; do '
    'port="$1"; shift; port_count=$((port_count - 1)); '
    '"$sshd_path" -D -e -p "$port" "$@" & pid=$!; '
    "sleep 0.1; "
    'if kill -0 "$pid" 2>/dev/null; then '
    f'printf "{SSHD_PORT_MARKER}%s\\n" "$port"; '
    'wait "$pid"; exit $?; '
    "fi; "
    'wait "$pid"; '
    "done; "
    "exit 1"
)


def absolute_sshd_command(arguments: list[str]) -> list[str]:
    """Run ``sshd`` by its resolved absolute path within a Pixi environment."""
    return ["sh", "-c", _EXEC_ABSOLUTE_SSHD, "sshd", *arguments]


def retrying_sshd_command(arguments: list[str], ports: tuple[int, ...]) -> list[str]:
    """Run foreground ``sshd``, retrying immediate startup failures by port."""
    if not ports:
        raise ValueError("at least one worker SSH port is required")
    return [
        "sh",
        "-c",
        _EXEC_RETRYING_SSHD,
        "sshd",
        str(len(ports)),
        *(str(port) for port in ports),
        *arguments,
    ]


def read_ed25519_public_key(path: Path) -> tuple[str, str]:
    """Read the two fields needed to authorize a generated worker key."""
    try:
        fields = path.read_text(encoding="ascii").split()
    except (OSError, UnicodeError) as error:
        raise RuntimeError(
            f"Could not read worker public key at {path}: {error}"
        ) from error
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise RuntimeError(f"The worker public key at {path} is malformed.")
    key_type, key_blob = fields[:2]
    if not key_blob or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        for character in key_blob
    ):
        raise RuntimeError(f"The worker public key at {path} is malformed.")
    return key_type, key_blob


def sshd_config_arguments(
    *,
    host_key: PurePosixPath,
    remote_username: str,
    authorized_key: tuple[str, str],
) -> list[str]:
    """Return the complete worker sshd configuration as command-line options."""
    key_type, key_blob = authorized_key
    settings = (
        "ListenAddress=0.0.0.0",
        # File Locations
        f"HostKey={host_key}",
        "AuthorizedKeysFile=none",
        "PidFile=none",
        # Authorized Keys
        f"AuthorizedKeysCommand=/bin/echo {key_type} {key_blob}",
        f"AuthorizedKeysCommandUser={remote_username}",
        # Authentication Schemes
        "StrictModes=yes",
        "PubkeyAuthentication=yes",
        "AuthenticationMethods=publickey",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "HostbasedAuthentication=no",
        "PermitEmptyPasswords=no",
        # Access Control
        "PermitRootLogin=no",
        f"AllowUsers={remote_username}",
        # Restrictions
        "PermitUserEnvironment=no",
        "AllowTcpForwarding=yes",
        "GatewayPorts=no",
        "AllowAgentForwarding=no",
        "X11Forwarding=no",
        "PermitTunnel=no",
        # Misc
        "UseDNS=no",
        "LogLevel=INFO",
        "Subsystem=sftp internal-sftp",
    )
    return [
        "-f",
        "/dev/null",
        *(part for setting in settings for part in ("-o", setting)),
    ]


class PromptMissingHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """
    Prompts the user whether to accept or reject a missing host key.
    If the user accepts, the key is added to the known hosts file.
    """

    def missing_host_key(self, client, hostname, key):
        console.print(
            f"[bold yellow]Warning[/bold yellow]: The host key for [bold purple]{hostname}[/bold purple] is not found in the known hosts file."
        )
        console.print(f"Key type: {key.get_name()}")
        console.print(f"Key fingerprint: {key.get_fingerprint().hex()}")
        user_accepts = Confirm.ask(
            "Do you want to accept this host key?: ",
            console=console,
            default="n",
            case_sensitive=False,
        )
        if user_accepts:
            client._host_keys.add(hostname, key.get_name(), key)

            if client._host_keys_filename is not None:
                client.save_host_keys(client._host_keys_filename)
                client._log(
                    DEBUG,
                    "Adding {} host key for {}: {}".format(
                        key.get_name(), hostname, hexlify(key.get_fingerprint())
                    ),
                )
            console.print(f"Host key for {hostname} added to known hosts.")
        else:
            raise paramiko.SSHException(f"Host key for {hostname} rejected by user.")


class InteractiveSSHClient(SSHClient):
    conn_info: ConnectionInfo

    def __init__(self, conn_info: ConnectionInfo):
        super().__init__(conn_info=conn_info)
        self.conn_info = conn_info

        self.load_system_host_keys()
        self.set_missing_host_key_policy(PromptMissingHostKeyPolicy())

    @staticmethod
    def _can_retry_with_password(error: paramiko.AuthenticationException) -> bool:
        if not isinstance(error, paramiko.BadAuthenticationType):
            return True
        return bool(
            {"password", "keyboard-interactive"}.intersection(error.allowed_types)
        )

    def interactive_connect(self) -> None:
        try:
            self.connect(
                hostname=self.conn_info.host,
                username=self.conn_info.user,
                password=self.conn_info.password,
            )
        except paramiko.AuthenticationException as error:
            if self.conn_info.password is not None or not self._can_retry_with_password(
                error
            ):
                raise

            # Agent and local-key authentication was unavailable or rejected.
            # Reconnect with a prompted password; Paramiko also uses it as the
            # response for single-prompt keyboard-interactive authentication.
            self.close()
            for attempt in range(1, 4):
                password = Prompt.ask(
                    f"Enter password for {self.conn_info.user}@{self.conn_info.host}",
                    password=True,
                    console=console,
                )
                try:
                    self.connect(
                        hostname=self.conn_info.host,
                        username=self.conn_info.user,
                        password=password,
                    )
                    return
                except paramiko.AuthenticationException as retry_error:
                    self.close()
                    if attempt == 3 or not self._can_retry_with_password(retry_error):
                        raise
                    console.print(
                        f"[bold red]Error[/bold red] [{attempt}/3]: Invalid "
                        "password. Try again or press Ctrl+C to abort."
                    )
