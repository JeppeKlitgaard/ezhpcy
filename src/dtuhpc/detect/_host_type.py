import os
import platform
from enum import StrEnum

from dtuhpc.config import config


class HostType(StrEnum):
    LOGIN_NODE = "login_node"
    COMPUTE_NODE = "compute_node"
    OTHER = "other"


def am_login_node() -> bool:
    """
    Check if the current host is a DTU HPC login node.
    """
    hostname = platform.node()
    return bool(config.hpc.login_node_pattern.match(hostname))


def am_compute_node() -> bool:
    """
    Check if the current host is a DTU HPC compute node.
    """
    am_lsf = os.environ.get("LSF_ENVDIR", default=None) is not None
    return am_lsf and not am_login_node()


def am_other() -> bool:
    """
    Check if the current host is neither a DTU HPC login node nor a compute node.
    """
    return not am_login_node() and not am_compute_node()


def get_host_type() -> HostType:
    """
    Determine the type of the current host.
    """
    if am_login_node():
        return HostType.LOGIN_NODE
    elif am_compute_node():
        return HostType.COMPUTE_NODE
    else:
        return HostType.OTHER
