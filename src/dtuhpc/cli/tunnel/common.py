import typer

from dtuhpc.detect import HostType, get_host_type


def local_machine_or_fail() -> None:
    """
    Fails the command if the current HostType is not OTHER.
    """
    if get_host_type() != HostType.OTHER:
        print("This command should be run on your local machine.")
        raise typer.Exit(code=1)
