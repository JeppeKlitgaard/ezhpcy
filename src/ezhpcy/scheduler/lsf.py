import math
import re
import time
from collections.abc import Callable, Mapping, Sequence

from ezhpcy.scheduler.base import (
    InteractiveJob,
    JobInfo,
    JobNotFoundError,
    JobSpec,
    JobState,
    RemoteProcess,
    Scheduler,
    SchedulerCommandError,
    SchedulerError,
    SchedulerOutputError,
)
from ezhpcy.scheduler.types import SchedulerType

_SUBMITTED_JOB_PATTERN = re.compile(r"Job <(?P<job_id>\d+)> is submitted")
_LSF_JOB_ID_PATTERN = re.compile(r"\d+")
_JOB_NOT_FOUND_PATTERN = re.compile(r"Job <[^>]+> is not found", re.IGNORECASE)
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

    def __init__(
        self,
        runner: Callable[[list[str]], str],
        process_starter: Callable[[list[str]], RemoteProcess] | None = None,
        *,
        interactive_application_profile: str | None = None,
        interactive_submission_environment: Mapping[str, str] | None = None,
        interactive_export_environment: Sequence[str] = (),
        resource_reserve_per_task: bool = False,
    ) -> None:
        self._runner = runner
        self._process_starter = process_starter
        self._interactive_application_profile = interactive_application_profile
        self._interactive_submission_environment = dict(
            interactive_submission_environment or {}
        )
        self._interactive_export_environment = tuple(interactive_export_environment)
        self._resource_reserve_per_task = resource_reserve_per_task

    @property
    def scheduler_type(self) -> SchedulerType:
        return SchedulerType.LSF

    def submit(self, spec: JobSpec) -> str:
        output = self._run(
            self._submit_command(
                spec,
                resource_reserve_per_task=self._resource_reserve_per_task,
            ),
            "submit a job",
        )
        match = _SUBMITTED_JOB_PATTERN.search(output)
        if match is None:
            raise SchedulerOutputError(
                f"could not find an LSF job ID in bsub output: {output.strip()!r}"
            )
        return match.group("job_id")

    def submit_interactive(
        self, spec: JobSpec, *, startup_timeout: float = 30.0
    ) -> InteractiveJob:
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if self._process_starter is None:
            raise SchedulerError("LSF interactive submission is not configured")

        command = self._submit_command(
            spec,
            interactive=True,
            application_profile=self._interactive_application_profile,
            export_environment=self._interactive_export_environment,
            submission_environment=self._interactive_submission_environment,
            resource_reserve_per_task=self._resource_reserve_per_task,
        )
        try:
            process = self._process_starter(command)
        except Exception as error:
            raise SchedulerCommandError(
                f"LSF failed to start interactive submission: {error}"
            ) from error

        output = bytearray()
        deadline = time.monotonic() + startup_timeout
        try:
            while True:
                while process.recv_ready():
                    output.extend(process.recv(64 * 1024))
                while process.recv_stderr_ready():
                    output.extend(process.recv_stderr(64 * 1024))

                decoded = output.decode(errors="replace")
                if match := _SUBMITTED_JOB_PATTERN.search(decoded):
                    return InteractiveJob(
                        job_id=match.group("job_id"),
                        process=process,
                        submission_output=decoded,
                        submission_command=tuple(command),
                    )
                if process.exit_status_ready():
                    status = process.recv_exit_status()
                    raise SchedulerCommandError(
                        "LSF interactive submission exited before returning a job "
                        f"ID ({status}): {decoded.strip()}"
                    )
                if time.monotonic() >= deadline:
                    raise SchedulerCommandError(
                        "LSF interactive submission did not return a job ID within "
                        f"{startup_timeout:g} seconds: {decoded.strip()}"
                    )
                time.sleep(0.05)
        except BaseException:
            process.close()
            raise

    def inspect(self, job_id: str) -> JobInfo:
        self._validate_job_id(job_id)
        try:
            output = self._runner(
                ["bjobs", "-a", "-noheader", "-o", _BJOBS_FORMAT, job_id]
            )
        except Exception as error:
            if _JOB_NOT_FOUND_PATTERN.search(str(error)):
                raise JobNotFoundError(job_id) from error
            raise SchedulerCommandError(
                f"LSF failed to inspect job {job_id!r}: {error}"
            ) from error
        return self._parse_job_info(job_id, output)

    def cancel(self, job_id: str) -> None:
        self._validate_job_id(job_id)
        self._run(["bkill", job_id], f"cancel job {job_id!r}")

    def _run(self, command: list[str], operation: str) -> str:
        try:
            return self._runner(command)
        except Exception as error:
            raise SchedulerCommandError(
                f"LSF failed to {operation}: {error}"
            ) from error

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if _LSF_JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise ValueError(f"invalid LSF job ID: {job_id!r}")

    @staticmethod
    def _submit_command(
        spec: JobSpec,
        *,
        interactive: bool = False,
        application_profile: str | None = None,
        export_environment: Sequence[str] = (),
        submission_environment: Mapping[str, str] | None = None,
        resource_reserve_per_task: bool = False,
    ) -> list[str]:
        command = ["bsub"]
        if interactive:
            command.append("-Is")
        if application_profile is not None:
            command.extend(["-app", application_profile])
        command.extend(["-n", str(spec.cores), "-R", "span[hosts=1]"])
        if export_environment:
            command.extend(["-env", ",".join(export_environment)])
        command.extend(["-J", spec.name])
        if spec.time_limit is not None:
            minutes = math.ceil(spec.time_limit.total_seconds() / 60)
            command.extend(["-W", str(minutes)])
        if spec.memory_bytes is not None:
            divisor = spec.cores if resource_reserve_per_task else 1
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
        if spec.environment:
            command.extend(
                ["env", *(f"{key}={value}" for key, value in spec.environment.items())]
            )
        command.extend(spec.command)
        if submission_environment:
            command = [
                "env",
                *(f"{key}={value}" for key, value in submission_environment.items()),
                *command,
            ]
        return command

    @staticmethod
    def _parse_job_info(job_id: str, output: str) -> JobInfo:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if len(lines) != 1:
            raise SchedulerOutputError(
                f"expected one bjobs record for {job_id!r}, got {len(lines)}"
            )

        fields = [field.strip() for field in lines[0].split("|")]
        if len(fields) != 4:
            raise SchedulerOutputError(
                f"expected four bjobs fields for {job_id!r}, got {len(fields)}"
            )
        reported_id, raw_state, raw_hosts, raw_exit_code = fields
        if reported_id != job_id:
            raise SchedulerOutputError(
                f"bjobs returned job {reported_id!r} while inspecting {job_id!r}"
            )

        state = _STATE_MAP.get(raw_state, JobState.UNKNOWN)
        hosts = ()
        if raw_hosts and raw_hosts != "-":
            hosts = tuple(
                dict.fromkeys(
                    re.sub(r"^\d+\*", "", host) for host in raw_hosts.split(":")
                )
            )

        exit_code = None
        if raw_exit_code and raw_exit_code != "-":
            try:
                exit_code = int(raw_exit_code)
            except ValueError as error:
                raise SchedulerOutputError(
                    f"invalid exit code for LSF job {job_id!r}: {raw_exit_code!r}"
                ) from error

        return JobInfo(
            job_id=job_id,
            state=state,
            raw_state=raw_state,
            execution_hosts=hosts,
            exit_code=exit_code,
        )
