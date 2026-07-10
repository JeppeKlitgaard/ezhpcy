from binascii import hexlify

from ezhpcy import console
from rich.prompt import Confirm, Prompt


import paramiko
from paramiko.common import DEBUG

from ezhpcy.config import ConnectionInfo


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


class InteractiveSSHClient(paramiko.SSHClient):
    conn_info: ConnectionInfo

    def __init__(self, conn_info: ConnectionInfo):
        super().__init__()
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
