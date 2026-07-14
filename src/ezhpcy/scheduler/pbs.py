import json
import math
import re
import shlex
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

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

_PBS_JOB_ID_PATTERN = re.compile(r"\d+(?:\.[A-Za-z0-9_.-]+)?")
_INTERACTIVE_JOB_PATTERN = re.compile(
    r"\bjob\s+(?P<job_id>\d+(?:\.[A-Za-z0-9_.-]+)?)\b",
    re.IGNORECASE,
)
_JOB_NOT_FOUND_PATTERN = re.compile(r"unknown\s+job\s+id", re.IGNORECASE)

_PENDING_STATES = {"Q", "T", "W"}
_RUNNING_STATES = {"B", "E", "R"}
_SUSPENDED_STATES = {"H", "S"}


class PBSScheduler(Scheduler):
    """PBS Professional adapter using its command-line client."""

    def __init__(
        self,
        runner: Callable[[list[str]], str],
        process_starter: Callable[[list[str]], RemoteProcess] | None = None,
        *,
        command_directory: PurePosixPath | None = None,
    ) -> None:
        self._runner = runner
        self._process_starter = process_starter
        self._qsub = self._executable(command_directory, "qsub")
        self._qstat = self._executable(command_directory, "qstat")
        self._qdel = self._executable(command_directory, "qdel")

    @property
    def scheduler_type(self) -> SchedulerType:
        return SchedulerType.PBS

    def submit(self, spec: JobSpec) -> str:
        output = self._run(
            self._submit_command(spec, executable=self._qsub), "submit a job"
        )
        job_id = output.strip()
        if _PBS_JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise SchedulerOutputError(
                f"could not find a PBS job ID in qsub output: {job_id!r}"
            )
        return job_id

    def submit_interactive(
        self, spec: JobSpec, *, startup_timeout: float = 30.0
    ) -> InteractiveJob:
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if self._process_starter is None:
            raise SchedulerError("PBS interactive submission is not configured")

        command = self._submit_command(
            spec,
            interactive=True,
            executable=self._qsub,
        )
        try:
            process = self._process_starter(command)
        except Exception as error:
            raise SchedulerCommandError(
                f"PBS failed to start interactive submission: {error}"
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
                if match := _INTERACTIVE_JOB_PATTERN.search(decoded):
                    payload = self._payload_shell_command(spec)

                    def start_payload() -> None:
                        if process.send(f"{payload}\n") <= 0:
                            raise SchedulerCommandError(
                                "PBS interactive shell did not accept the worker command"
                            )

                    return InteractiveJob(
                        job_id=match.group("job_id"),
                        process=process,
                        submission_output=decoded,
                        submission_command=tuple(command),
                        payload_starter=start_payload,
                    )
                if process.exit_status_ready():
                    status = process.recv_exit_status()
                    raise SchedulerCommandError(
                        "PBS interactive submission exited before returning a job "
                        f"ID ({status}): {decoded.strip()}"
                    )
                if time.monotonic() >= deadline:
                    raise SchedulerCommandError(
                        "PBS interactive submission did not return a job ID within "
                        f"{startup_timeout:g} seconds: {decoded.strip()}"
                    )
                time.sleep(0.05)
        except BaseException:
            process.close()
            raise

    def inspect(self, job_id: str) -> JobInfo:
        self._validate_job_id(job_id)
        try:
            output = self._runner([self._qstat, "-f", "-F", "json", job_id])
        except Exception as active_error:
            if not _JOB_NOT_FOUND_PATTERN.search(str(active_error)):
                raise SchedulerCommandError(
                    f"PBS failed to inspect job {job_id!r}: {active_error}"
                ) from active_error
            try:
                output = self._runner([self._qstat, "-x", "-f", "-F", "json", job_id])
            except Exception as history_error:
                if _JOB_NOT_FOUND_PATTERN.search(str(history_error)):
                    raise JobNotFoundError(job_id) from history_error
                raise SchedulerCommandError(
                    f"PBS failed to inspect historical job {job_id!r}: {history_error}"
                ) from history_error
        return self._parse_job_info(job_id, output)

    def cancel(self, job_id: str) -> None:
        self._validate_job_id(job_id)
        self._run([self._qdel, job_id], f"cancel job {job_id!r}")

    def _run(self, command: list[str], operation: str) -> str:
        try:
            return self._runner(command)
        except Exception as error:
            raise SchedulerCommandError(
                f"PBS failed to {operation}: {error}"
            ) from error

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if _PBS_JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise ValueError(f"invalid PBS job ID: {job_id!r}")

    @staticmethod
    def _executable(directory: PurePosixPath | None, name: str) -> str:
        return str(directory / name) if directory is not None else name

    @staticmethod
    def _submit_command(
        spec: JobSpec,
        *,
        interactive: bool = False,
        executable: str = "qsub",
    ) -> list[str]:
        command = [executable]
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
        if not interactive:
            if spec.stdout_path is not None:
                command.extend(["-o", str(spec.stdout_path)])
            if spec.stderr_path is not None:
                command.extend(["-e", str(spec.stderr_path)])
            if spec.working_directory is None:
                command.extend(["--", *PBSScheduler._payload_command(spec)])
            else:
                command.extend(
                    ["--", "bash", "-lc", PBSScheduler._payload_shell_command(spec)]
                )
        return command

    @staticmethod
    def _payload_command(spec: JobSpec) -> list[str]:
        command = []
        if spec.environment:
            command.extend(
                ["env", *(f"{key}={value}" for key, value in spec.environment.items())]
            )
        command.extend(spec.command)
        return command

    @staticmethod
    def _payload_shell_command(spec: JobSpec) -> str:
        payload = f"exec {shlex.join(PBSScheduler._payload_command(spec))}"
        if spec.working_directory is None:
            return payload
        return f"cd {shlex.quote(str(spec.working_directory))} && {payload}"

    @staticmethod
    def _parse_job_info(job_id: str, output: str) -> JobInfo:
        try:
            document = json.loads(output)
            jobs = document["Jobs"]
            record = PBSScheduler._find_job_record(job_id, jobs)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            excerpt = output.strip()
            if len(excerpt) > 1000:
                excerpt = f"{excerpt[:1000]}..."
            raise SchedulerOutputError(
                f"could not parse PBS qstat output for {job_id!r}: {excerpt!r}"
            ) from error

        raw_state = str(record.get("job_state", ""))
        exit_code = PBSScheduler._parse_exit_code(record.get("Exit_status"))
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

        hosts = ()
        raw_hosts = str(record.get("exec_host", ""))
        if raw_hosts:
            hosts = tuple(
                dict.fromkeys(
                    host.partition("/")[0]
                    for host in raw_hosts.split("+")
                    if host.partition("/")[0]
                )
            )
        return JobInfo(
            job_id=job_id,
            state=state,
            raw_state=raw_state,
            execution_hosts=hosts,
            exit_code=exit_code,
        )

    @staticmethod
    def _find_job_record(job_id: str, jobs: object) -> dict[str, Any]:
        if not isinstance(jobs, dict):
            raise TypeError("PBS Jobs value is not an object")
        record = jobs.get(job_id)
        if record is None:
            canonical_ids = [key for key in jobs if key.startswith(f"{job_id}.")]
            if len(canonical_ids) == 1:
                record = jobs[canonical_ids[0]]
            elif len(jobs) == 1:
                record = next(iter(jobs.values()))
            else:
                raise KeyError(job_id)
        if not isinstance(record, dict):
            raise TypeError("PBS job record is not an object")
        return record

    @staticmethod
    def _parse_exit_code(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise SchedulerOutputError(f"invalid PBS exit status: {value!r}") from error
