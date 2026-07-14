import os

from ezhpcy.scheduler.types import SchedulerType


def am_lsf() -> bool:
    """Check whether the current host is using the LSF scheduler."""
    return os.environ.get("LSF_ENVDIR", default=None) is not None


def am_pbs() -> bool:
    """Check whether the current process belongs to a PBS job."""
    return os.environ.get("PBS_JOBID") is not None


def get_scheduler_type() -> SchedulerType | None:
    """Determine the scheduler used by the current host or job."""
    if am_pbs():
        return SchedulerType.PBS
    elif am_lsf():
        return SchedulerType.LSF
    else:
        return None
