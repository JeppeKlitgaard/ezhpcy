import os
from enum import StrEnum


class HostType(StrEnum):
    COMPUTE_NODE = "compute_node"
    OTHER = "other"


def am_compute_node() -> bool:
    """Check whether the current host is an HPC compute node."""
    has_scheduler_job = any(
        os.environ.get(variable) is not None
        for variable in ("LSB_JOBID", "LSF_ENVDIR", "PBS_JOBID")
    )
    return has_scheduler_job


def am_other() -> bool:
    """Check whether the current host is outside a scheduler allocation."""
    return not am_compute_node()


def get_host_type() -> HostType:
    """Determine the type of the current host."""
    if am_compute_node():
        return HostType.COMPUTE_NODE
    return HostType.OTHER
