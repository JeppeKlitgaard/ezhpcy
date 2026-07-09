import os
from enum import StrEnum


class SchedulerType(StrEnum):
    LSF = "LSF"
    UNKNOWN = "UNKNOWN"


def am_lsf() -> bool:
    """
    Check if the current host is using the LSF scheduler.
    """
    return os.environ.get("LSF_ENVDIR", default=None) is not None


def get_scheduler_type() -> SchedulerType:
    """
    Determine the type of scheduler used on the current host.
    """
    if am_lsf():
        return SchedulerType.LSF
    else:
        return SchedulerType.UNKNOWN
