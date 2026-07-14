import os
import platform
from enum import StrEnum

from ezhpcy.config import config


class HostType(StrEnum):
    LOGIN_NODE = "login_node"
    COMPUTE_NODE = "compute_node"
    OTHER = "other"


def am_login_node() -> bool:
    """Check whether the current host is an HPC login node."""
    hostname = platform.node()
    return bool(config.hpc.login_node_pattern.match(hostname))


def am_compute_node() -> bool:
    """Check whether the current host is an HPC compute node."""
    has_scheduler_job = any(
        os.environ.get(variable) is not None
        for variable in ("LSB_JOBID", "LSF_ENVDIR", "PBS_JOBID")
    )
    return has_scheduler_job and not am_login_node()


def am_other() -> bool:
    """Check whether the current host is neither a login nor compute node."""
    return not am_login_node() and not am_compute_node()


def get_host_type() -> HostType:
    """Determine the type of the current host."""
    if am_login_node():
        return HostType.LOGIN_NODE
    elif am_compute_node():
        return HostType.COMPUTE_NODE
    else:
        return HostType.OTHER
