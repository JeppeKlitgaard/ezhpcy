from importlib.metadata import PackageNotFoundError, version
from urllib.parse import quote

import machineid


def ezhpcy_version() -> str:
    try:
        return version("ezhpcy")
    except PackageNotFoundError:
        return "unknown"


def local_machine_id() -> str:
    """
    Return a unique identifier for the local machine.

    This is used to identify the machine such that we can keep ssh keys on the HPC that are unique to the local machine.
    """
    return machineid.hashed_id("ezhpcy")


def ssh_connection_id(user: str, host: str) -> str:
    """Return a filesystem-safe, human-readable identity for an SSH endpoint."""
    normalized_host = host.rstrip(".").lower()
    return f"{quote(user, safe='')}@{quote(normalized_host, safe='')}"
