import re
lazy import math
lazy import shlex
lazy from collections.abc import Sequence

from ezhpcy.scheduler.base import JobInfo, JobSpec, JobState, Scheduler, SchedulerError
from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.ssh import RemoteHost
lazy from ezhpcy.types import LSFConfig

_SUBMITTED_JOB_PATTERN = re.compile(r"Job <(?P<job_id>\d+)> is submitted")
_BJOBS_FORMAT = "jobid stat exec_host exit_code delimiter='|'"
_MEBIBYTE = 1024**2

_STATE_MAP = {
    "PEND": JobState.PENDING,
    "WAIT": JobState.PENDING,
    "PROV": JobState.PENDING,
    "RUN": JobState.RUNNING,
    "PSUSP": JobState.SUSPENDED,
    "USUSP": JobState.SUSPENDED,
    "SSUSP": JobState.SUSPENDED,
    "DONE": JobState.SUCCEEDED,
    "POST_DONE": JobState.SUCCEEDED,
    "EXIT": JobState.FAILED,
    "POST_ERR": JobState.FAILED,
    "ZOMBI": JobState.FAILED,
    "UNKWN": JobState.UNKNOWN,
}


class LSFScheduler(Scheduler):
    """IBM Spectrum LSF adapter using its command-line client."""

    scheduler_type = SchedulerType.LSF
    interactive_job_id_pattern = _SUBMITTED_JOB_PATTERN

    def __init__(
        self,
        host: RemoteHost,
        lsf: LSFConfig,
        *,
        interactive_submission_command: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            host, interactive_submission_command=interactive_submission_command
        )
        self._lsf = lsf

    async def submit(self, spec: JobSpec) -> str:
        # As a script on `bsub`'s stdin: given the command as arguments, `bsub`
        # joins them with spaces, losing their quoting.
        script = f"#!/usr/bin/env bash\nexec {shlex.join(spec.command)}\n"
        output = await self._run(
            self._bsub_command(spec, interactive=False), "submit a job", stdin=script
        )
        return self._parse_job_id(output)

    async def inspect(self, job_id: str) -> JobInfo:
        output = await self._run(
            ["bjobs", "-a", "-noheader", "-o", _BJOBS_FORMAT, job_id],
            f"inspect job {job_id!r}",
        )
        try:
            [line] = output.strip().splitlines()
            _, raw_state, raw_host, raw_exit_code = (
                field.strip() for field in line.split("|")
            )
            exit_code = None if raw_exit_code in {"", "-"} else int(raw_exit_code)
        except ValueError as error:
            raise SchedulerError(
                f"could not parse bjobs output for job {job_id!r}: {output.strip()!r}"
            ) from error
        return JobInfo(
            state=_STATE_MAP.get(raw_state, JobState.UNKNOWN),
            raw_state=raw_state,
            # `4*node1:4*node2`, though the job asks for a single host.
            host=(
                None
                if raw_host in {"", "-"}
                else re.sub(r"^\d+\*", "", raw_host.split(":")[0])
            ),
            exit_code=exit_code,
        )

    def _interactive_command(self, spec: JobSpec) -> list[str]:
        command = [*self._bsub_command(spec, interactive=True), "/bin/sh"]
        if self._lsf.submission_environment:
            command = [
                "env",
                *(
                    f"{key}={value}"
                    for key, value in self._lsf.submission_environment.items()
                ),
                *command,
            ]
        return command

    def _cancel_command(self, job_id: str) -> list[str]:
        return ["bkill", job_id]

    def _parse_job_id(self, output: str) -> str:
        match = _SUBMITTED_JOB_PATTERN.search(output)
        if match is None:
            raise SchedulerError(
                f"could not find an LSF job ID in bsub output: {output.strip()!r}"
            )
        return match.group("job_id")

    def _bsub_command(self, spec: JobSpec, *, interactive: bool) -> list[str]:
        """``bsub`` and its options, without the job to run."""
        command = ["bsub"]
        if interactive:
            command.append("-Is")
            if self._lsf.application_profile is not None:
                command.extend(["-app", self._lsf.application_profile])
        command.extend(["-n", str(spec.cores), "-R", "span[hosts=1]"])
        if interactive and self._lsf.export_environment:
            command.extend(["-env", ",".join(self._lsf.export_environment)])
        command.extend(["-J", spec.name])
        if spec.time_limit is not None:
            minutes = math.ceil(spec.time_limit.total_seconds() / 60)
            command.extend(["-W", str(minutes)])
        if spec.memory_bytes is not None:
            divisor = spec.cores if self._lsf.resource_reserve_per_task else 1
            reservation_unit = divisor * _MEBIBYTE
            memory_mib = (spec.memory_bytes + reservation_unit - 1) // reservation_unit
            command.extend(["-R", f"rusage[mem={memory_mib}MB]"])
        if spec.gpus:
            command.extend(["-gpu", f"num={spec.gpus}/host"])
        if spec.exclusive:
            command.append("-x")
        if spec.queue is not None:
            command.extend(["-q", spec.queue])
        if spec.working_directory is not None:
            command.extend(["-cwd", str(spec.working_directory)])
        if spec.stdout_path is not None:
            command.extend(["-o", str(spec.stdout_path)])
        if spec.stderr_path is not None:
            command.extend(["-e", str(spec.stderr_path)])
        return command
