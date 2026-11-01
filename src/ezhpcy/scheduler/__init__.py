"""Scheduler adapters, and the one a profile uses."""

lazy from ezhpcy.scheduler.base import Scheduler
lazy from ezhpcy.scheduler.lsf import LSFScheduler
lazy from ezhpcy.scheduler.pbs import PBSScheduler
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.ssh import RemoteHost
lazy from ezhpcy.types import JobConfig


def create_scheduler(config: JobConfig, host: RemoteHost) -> Scheduler:
    """The adapter for the job's scheduler, running its commands on `host`."""
    command = config.scheduler.interactive_submission_command
    match config.scheduler.type:
        case SchedulerType.LSF:
            return LSFScheduler(
                host, config.lsf, interactive_submission_command=command
            )
        case SchedulerType.PBS:
            return PBSScheduler(
                host, config.pbs, interactive_submission_command=command
            )
