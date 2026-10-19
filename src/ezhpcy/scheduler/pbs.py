import re
lazy import json
lazy import math
lazy from collections.abc import Sequence
lazy from pathlib import PurePosixPath

lazy import asyncssh

from ezhpcy.scheduler.base import JobInfo, JobSpec, JobState, Scheduler, SchedulerError
from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.ssh import RemoteHost
lazy from ezhpcy.types import PBSConfig

_JOB_ID_PATTERN = re.compile(r"\d+(?:\.[A-Za-z0-9_.-]+)?")
_JOB_NOT_FOUND_PATTERN = re.compile(r"unknown\s+job\s+id", re.IGNORECASE)

_PENDING_STATES = {"Q", "T", "W"}
_RUNNING_STATES = {"B", "E", "R"}
_SUSPENDED_STATES = {"H", "S"}
# A job PBS no longer lists, so its state and exit status are unknown.
_ENDED = JobInfo(state=JobState.ENDED, raw_state="")


class PBSScheduler(Scheduler):
    """PBS Professional adapter using its command-line client."""

    scheduler_type = SchedulerType.PBS
    interactive_job_id_pattern = re.compile(
        rf"\bjob\s+(?P<job_id>{_JOB_ID_PATTERN.pattern})\b", re.IGNORECASE
    )

    def __init__(
        self,
        host: RemoteHost,
        pbs: PBSConfig,
        *,
        interactive_submission_command: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            host, interactive_submission_command=interactive_submission_command
        )
        self._qsub = _executable(pbs.command_directory, "qsub")
        self._qstat = _executable(pbs.command_directory, "qstat")
        self._qdel = _executable(pbs.command_directory, "qdel")

    async def submit(self, spec: JobSpec) -> str:
        output = await self._run(self._submit_command(spec), t"submit a job")
        return self._parse_job_id(output)

    async def inspect(self, job_id: str) -> JobInfo:
        try:
            output = await self._run(
                [self._qstat, "-f", "-F", "json", job_id],
                t"inspect job {job_id:detail}",
            )
        except SchedulerError as error:
            if not _job_not_found(error):
                raise
            # PBS drops a job from `qstat` once it has ended. Only a server that
            # keeps job history still lists it, with `qstat -x`.
            try:
                output = await self._run(
                    [self._qstat, "-x", "-f", "-F", "json", job_id],
                    t"inspect historical job {job_id:detail}",
                )
            except SchedulerError as history_error:
                if not _job_not_found(history_error):
                    raise
                return _ENDED
        return _parse_job_info(job_id, output)

    def _submit_command(self, spec: JobSpec) -> list[str]:
        command = self._qsub_command(spec, interactive=False)
        if spec.stdout_path is not None:
            command.extend(["-o", str(spec.stdout_path)])
        if spec.stderr_path is not None:
            command.extend(["-e", str(spec.stderr_path)])
        if spec.working_directory is None:
            command.extend(["--", *spec.command])
        else:
            command.extend(["--", "bash", "-lc", self._job_shell_command(spec)])
        return command

    def _interactive_command(self, spec: JobSpec) -> list[str]:
        return self._qsub_command(spec, interactive=True)

    def _cancel_command(self, job_id: str) -> list[str]:
        return [self._qdel, job_id]

    def _parse_job_id(self, output: str) -> str:
        job_id = output.strip()
        if _JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise SchedulerError(
                t"could not find a PBS job ID in qsub output: {job_id!r:detail}"
            )
        return job_id

    def _qsub_command(self, spec: JobSpec, *, interactive: bool) -> list[str]:
        """``qsub`` and its options, without the job to run."""
        command = [self._qsub]
        if interactive:
            command.append("-I")
        command.extend(["-N", spec.name])
        if spec.queue is not None:
            command.extend(["-q", spec.queue])

        resources = [f"ncpus={spec.cores}"]
        if spec.memory_bytes is not None:
            resources.append(f"mem={spec.memory_bytes}b")
        if spec.gpus:
            resources.append(f"ngpus={spec.gpus}")
        command.extend(["-l", f"select=1:{':'.join(resources)}"])
        if spec.exclusive:
            command.extend(["-l", "place=exclhost"])
        if spec.time_limit is not None:
            seconds = math.ceil(spec.time_limit.total_seconds())
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            command.extend(["-l", f"walltime={hours:02d}:{minutes:02d}:{seconds:02d}"])
        return command


def _executable(directory: PurePosixPath | None, name: str) -> str:
    return str(directory / name) if directory is not None else name


def _job_not_found(error: SchedulerError) -> bool:
    """Whether `qstat` failed because the job isn't (or is no longer) queued."""
    cause = error.__cause__
    return isinstance(cause, asyncssh.ProcessError) and bool(
        _JOB_NOT_FOUND_PATTERN.search(str(cause.stderr))
    )


def _parse_job_info(job_id: str, output: str) -> JobInfo:
    try:
        document = json.loads(output)
        if isinstance(document, dict) and "Jobs" not in document:
            # A server without job history lists no ended job, even with `-x`
            # (DTU's: it exits 0, saying so on stderr).
            return _ENDED
        # The only job in the output. Its key is the full ID, which may be longer
        # than `job_id` (`1.server.example.org` for `1.server`).
        [record] = document["Jobs"].values()
        raw_state = str(record.get("job_state", ""))
        raw_exit_code = record.get("Exit_status")
        exit_code = None if raw_exit_code is None else int(raw_exit_code)
        # `node1/0*2+node1/1`, though the job asks for a single host.
        host = str(record.get("exec_host", "")).split("+")[0].partition("/")[0]
        # E.g. `Not Running: Insufficient amount of resource: ncpus`. It says
        # other things once the job runs (`Job run at … on …`).
        comment = str(record.get("comment", "")).strip()
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        excerpt = output.strip()
        if len(excerpt) > 1000:
            excerpt = f"{excerpt[:1000]}..."
        raise SchedulerError(
            t"could not parse PBS qstat output for job {job_id:detail}: "
            t"{excerpt!r:detail}"
        ) from error

    if raw_state in _PENDING_STATES:
        state = JobState.PENDING
    elif raw_state in _RUNNING_STATES:
        state = JobState.RUNNING
    elif raw_state in _SUSPENDED_STATES:
        state = JobState.SUSPENDED
    elif raw_state == "F":
        state = JobState.SUCCEEDED if exit_code == 0 else JobState.FAILED
    elif raw_state == "X":
        state = JobState.FAILED
    else:
        state = JobState.UNKNOWN
    return JobInfo(
        state=state,
        raw_state=raw_state,
        host=host or None,
        exit_code=exit_code,
        pending_reason=(comment or None) if state is JobState.PENDING else None,
    )
