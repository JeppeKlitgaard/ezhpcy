import typer

from ezhpcy.cli.tunnel.broker import broker_cmd
from ezhpcy.cli.tunnel.compute import compute_cmd
from ezhpcy.cli.tunnel.install import install_cmd
from ezhpcy.cli.tunnel.reinstall import reinstall_cmd
from ezhpcy.cli.tunnel.relay import relay_cmd
from ezhpcy.cli.tunnel.ssh_config import ssh_config_cmd
from ezhpcy.cli.tunnel.uninstall import uninstall_cmd
from ezhpcy.cli.utils.alias import AliasGroup

tunnel_app = typer.Typer(
    cls=AliasGroup,
    help="Open tunnels for HPC workflows.",
    no_args_is_help=True,
)


tunnel_app.command(name="install", help="Install ezhpcy on HPC.")(install_cmd)
tunnel_app.command(name="reinstall", help="Uninstall, then install ezhpcy on HPC.")(
    reinstall_cmd
)
tunnel_app.command(name="uninstall", help="Uninstall ezhpcy from HPC.")(uninstall_cmd)
tunnel_app.command(
    name="compute, c",
    help="Allocate a compute node and start its SSH tunnel.",
)(compute_cmd)
tunnel_app.command(
    name="relay", help="Relay OpenSSH to a worker through the login node."
)(relay_cmd)
tunnel_app.command(name="broker", help="Run the foreground worker-stream broker.")(
    broker_cmd
)
tunnel_app.command(
    name="ssh-config",
    help="Print the OpenSSH configuration for the broker-backed worker.",
)(ssh_config_cmd)
