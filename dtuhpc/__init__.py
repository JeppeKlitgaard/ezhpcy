from pathlib import Path
import paramiko
from dataclasses import dataclass


@dataclass
class Paths:
    home: Path
    cache: Path
    code_cli: Path

    @classmethod
    def from_home(cls, home: Path):
        return cls(
            home=home,
            cache=home / ".cache" / "dtunnel",
            code_cli=home / ".cache" / "dtunnel" / "code"
        )


def get_paths(client: paramiko.client.SSHClient):
        # Get the home directory of the user
        _, stdout, stderr = client.exec_command("echo $HOME")
        assert stdout.channel.recv_exit_status() == 0, f"Failed to get home directory:, {stderr.read().decode()}"

        home_dir = Path(stdout.read().decode().strip())

        return Paths.from_home(home_dir)

HOME_DIR = Path.home()
CACHE_DIR = HOME_DIR / ".cache" / "dtunnel"
CODE_CLI_PATH = CACHE_DIR / "code"

VSCODE_CLI_URL = "https://code.visualstudio.com/sha/download?build=stable&os=cli-alpine-x64"

def check_vscode_cli_installed(client: paramiko.client.SSHClient):
    paths = get_paths(client)

    cmd = f"test -e {paths.code_cli.as_posix()}"
    _, stdout, _ = client.exec_command(cmd)

    exists = stdout.channel.recv_exit_status() == 0

    if exists:
        return True
    else :
        return False


def install_vscode_cli(client: paramiko.client.SSHClient):
    assert not check_vscode_cli_installed(client), "VSCode CLI is already installed"
    paths = get_paths(client)

    cmd_mkdir = f"mkdir -p {paths.cache.as_posix()}"
    _, stdout, stderr = client.exec_command(cmd_mkdir)
    assert stdout.channel.recv_exit_status() == 0, f"Failed to create cache directory:, {stderr.read().decode()}"

    cmd_url = f"curl -Lk '{VSCODE_CLI_URL}' --output {paths.cache.as_posix()}/vscode_cli.tar.gz"
    _, stdout, stderr = client.exec_command(cmd_url)
    assert stdout.channel.recv_exit_status() == 0, f"Failed to download VSCode CLI:, {stderr.read().decode()}"

    cmd_extract = f"tar -xf {paths.cache.as_posix()}/vscode_cli.tar.gz -C {paths.cache.as_posix()}"
    _, stdout, stderr = client.exec_command(cmd_extract)
    assert stdout.channel.recv_exit_status() == 0, f"Failed to extract VSCode CLI:, {stderr.read().decode()}"

    cmd_rm_temp = f"rm {paths.cache.as_posix()}/vscode_cli.tar.gz"
    _, stdout, stderr = client.exec_command(cmd_rm_temp)
    assert stdout.channel.recv_exit_status() == 0, f"Failed to remove temporary file:, {stderr.read().decode()}"


