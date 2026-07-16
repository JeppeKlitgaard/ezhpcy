import typer

from ezhpcy.cli.broker import broker_cmd
from ezhpcy.cli.compute import compute_cmd
from ezhpcy.cli.config import config_app
from ezhpcy.cli.info import info_cmd
from ezhpcy.cli.keyring import keyring_app
from ezhpcy.cli.list_profiles import list_profiles_cmd
from ezhpcy.cli.provision import provision_cmd
from ezhpcy.cli.proxy import proxy_cmd
from ezhpcy.cli.prune import prune_cmd
from ezhpcy.cli.relay import relay_cmd
from ezhpcy.cli.ssh_config import ssh_config_cmd
from ezhpcy.cli.utils.alias import AliasGroup
from ezhpcy.cli.version import version_cmd

app = typer.Typer(
    cls=AliasGroup,
    help="Utilities for working with HPC facilities.",
    no_args_is_help=True,
)

app.add_typer(config_app, name="config")
app.add_typer(keyring_app, name="keyring")
app.command(name="info", help="Show debug information.")(info_cmd)
app.command(name="version", help="Show the EzHPCy version.")(version_cmd)
app.command(name="list-profiles", help="List configured profiles.")(list_profiles_cmd)
app.command(name="provision", help="Provision EzHPCy worker infrastructure.")(
    provision_cmd
)
app.command(name="prune", help="Prune EzHPCy-managed remote data.")(prune_cmd)
app.command(
    name="compute, c",
    help="Allocate a compute node and start its SSH tunnel.",
)(compute_cmd)
app.command(name="relay", help="Relay OpenSSH to a worker through the login node.")(
    relay_cmd
)
app.command(name="broker", help="Run the foreground worker-stream broker.")(broker_cmd)
app.command(
    name="ssh-config",
    help="Print the OpenSSH configuration for the broker-backed worker.",
)(ssh_config_cmd)
app.command(name="proxy", help="Proxy SSH bytes through a running tunnel broker.")(
    proxy_cmd
)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
