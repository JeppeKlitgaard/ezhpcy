from enum import StrEnum


class SchedulerType(StrEnum):
    """The batch schedulers EzHPCy submits worker jobs to."""

    LSF = "LSF"
    PBS = "PBS"
