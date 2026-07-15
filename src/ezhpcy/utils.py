from importlib.metadata import PackageNotFoundError, version

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
