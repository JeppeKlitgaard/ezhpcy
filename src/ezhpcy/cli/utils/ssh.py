from binascii import hexlify
from pathlib import Path

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


def absolute_sshd_command(arguments: list[str]) -> list[str]:
    """Run ``sshd`` by its resolved absolute path within a Pixi environment."""
    return ["sh", "-c", _EXEC_ABSOLUTE_SSHD, "sshd", *arguments]


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

    def interactive_connect(self):
        try:
            self.connect(
                hostname=self.conn_info.host,
                username=self.conn_info.user,
                password=self.conn_info.password,
            )
        except paramiko.BadAuthenticationType as e:
            # We just needed to provide a password, do so interactively
            if "password" in e.allowed_types and self.conn_info.password is None:
                attempts = 1
                while True:
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
                        break
                    except paramiko.AuthenticationException as e:
                        attempts += 1

                        if attempts > 3:
                            raise e

                        console.print(
                            f"[bold red]Error[/bold red] [{attempts}/3]: Invalid password. Try again or press Ctrl+C to abort."
                        )

        except paramiko.SSHException as e:
            console.print(f"[bold red]Error[/bold red]: {e}")
