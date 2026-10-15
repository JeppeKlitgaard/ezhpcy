"""Scheduler adapters, and the one a profile uses."""

lazy from ezhpcy.scheduler.base import Scheduler
lazy from ezhpcy.scheduler.lsf import LSFScheduler
lazy from ezhpcy.scheduler.pbs import PBSScheduler
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.ssh import RemoteHost
lazy from ezhpcy.types import ResolvedConfig


def create_scheduler(resolved: ResolvedConfig, host: RemoteHost) -> Scheduler:
    """The adapter for the profile's scheduler, running its commands on `host`."""
    command = resolved.scheduler.interactive_submission_command
    match resolved.scheduler.type:
        case SchedulerType.LSF:
            return LSFScheduler(
                host, resolved.lsf, interactive_submission_command=command
            )
        case SchedulerType.PBS:
            return PBSScheduler(
                host, resolved.pbs, interactive_submission_command=command
            )
        case None:
            raise ValueError("scheduler.type must be set")
