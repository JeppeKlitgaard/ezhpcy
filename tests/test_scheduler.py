import json
import shlex
from datetime import timedelta
from pathlib import PurePosixPath

import pytest

from ezhpcy.scheduler.base import (
    InteractiveJob,
    JobNotFoundError,
    JobSpec,
    JobState,
    SchedulerCommandError,
    SchedulerOutputError,
)
from ezhpcy.scheduler.lsf import LSFScheduler
from ezhpcy.scheduler.pbs import PBSScheduler

_MEBIBYTE = 1024**2
_COMMAND = "/home/user/bin/worker"


class FakeRunner:
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.commands: list[list[str]] = []
        self.error: Exception | None = None

    def __call__(self, command: list[str]) -> str:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.output


class FakeProcess:
    def __init__(
        self,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        *,
        exit_status: int | None = None,
    ) -> None:
        self.stdout = stdout or []
        self.stderr = stderr or []
        self.exit_status = exit_status
        self.closed = False
        self.sent: list[bytes | str] = []

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv(self, _size: int) -> bytes:
        return self.stdout.pop(0)

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv_stderr(self, _size: int) -> bytes:
        return self.stderr.pop(0)

    def exit_status_ready(self) -> bool:
        return self.exit_status is not None

    def recv_exit_status(self) -> int:
        assert self.exit_status is not None
        return self.exit_status

    def send(self, data: bytes | str) -> int:
        self.sent.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


def test_lsf_submission_builds_portable_job_spec() -> None:
    runner = FakeRunner("Job <31415> is submitted to queue <normal>.\n")
    scheduler = LSFScheduler(runner)
    spec = JobSpec(
        command=(_COMMAND, "34321"),
        name="ezhpcy-worker",
        time_limit=timedelta(hours=1, seconds=1),
        memory_bytes=1024 * _MEBIBYTE,
        cores=4,
        gpus=2,
        exclusive=True,
        queue="normal",
        working_directory=PurePosixPath("/home/user"),
        stdout_path=PurePosixPath("/home/user/worker.out"),
        stderr_path=PurePosixPath("/home/user/worker.err"),
        environment={"EZHPCY_PROFILE": "default"},
    )

    assert scheduler.submit(spec) == "31415"
    assert runner.commands == [
        [
            "bsub",
            "-n",
            "4",
            "-R",
            "span[hosts=1]",
            "-J",
            "ezhpcy-worker",
            "-W",
            "61",
            "-R",
            "rusage[mem=1024MB]",
            "-gpu",
            "num=2/host",
            "-x",
            "-q",
            "normal",
            "-cwd",
            "/home/user",
            "-o",
            "/home/user/worker.out",
            "-e",
            "/home/user/worker.err",
            "env",
            "EZHPCY_PROFILE=default",
            _COMMAND,
            "34321",
        ]
    ]


def test_lsf_script_submission_streams_job_body_to_bsub() -> None:
    calls: list[tuple[list[str], str]] = []

    def run_script(command: list[str], script: str) -> str:
        calls.append((command, script))
        return "Job <31415> is submitted to queue <normal>.\n"

    scheduler = LSFScheduler(FakeRunner(), script_runner=run_script)
    spec = JobSpec(
        command=("bash", "-s", "--", "54321"),
        name="ezhpcy-worker",
        cores=4,
        queue="normal",
    )

    assert (
        scheduler.submit_script(spec, "#!/usr/bin/env bash\necho worker\n") == "31415"
    )
    assert calls == [
        (
            [
                "bsub",
                "-n",
                "4",
                "-R",
                "span[hosts=1]",
                "-J",
                "ezhpcy-worker",
                "-q",
                "normal",
            ],
            "#!/usr/bin/env bash\necho worker\n",
        )
    ]


def test_lsf_interactive_submission_keeps_process_and_uses_site_profile() -> None:
    runner = FakeRunner()
    process = FakeProcess(stdout=[b"Job <2718> is submitted to queue <hpcint>.\r\n"])
    commands: list[list[str]] = []

    def start(command: list[str]) -> FakeProcess:
        commands.append(command)
        return process

    scheduler = LSFScheduler(
        runner,
        start,
        interactive_application_profile="qrsh",
        interactive_submission_environment={
            "ESUB_BYPASS": "1",
            "ESUB_QUIET": "1",
            "LSF_QRSH": "true",
        },
        interactive_export_environment=("TERM", "LSF_QRSH"),
        resource_reserve_per_task=True,
    )
    spec = JobSpec(
        command=(_COMMAND, "54321"),
        name="ezhpcy-worker",
        time_limit=timedelta(minutes=60),
        memory_bytes=1024 * _MEBIBYTE,
        cores=4,
        queue="hpcint",
    )

    job = scheduler.submit_interactive(spec)

    assert isinstance(job, InteractiveJob)
    assert job.job_id == "2718"
    assert job.process is process
    assert job.submission_command == tuple(commands[0])
    assert process.sent == []

    job.start_command()

    assert process.sent == [f"exec {_COMMAND} 54321\n"]
    assert commands == [
        [
            "env",
            "ESUB_BYPASS=1",
            "ESUB_QUIET=1",
            "LSF_QRSH=true",
            "bsub",
            "-Is",
            "-app",
            "qrsh",
            "-n",
            "4",
            "-R",
            "span[hosts=1]",
            "-env",
            "TERM,LSF_QRSH",
            "-J",
            "ezhpcy-worker",
            "-W",
            "60",
            "-R",
            "rusage[mem=256MB]",
            "-q",
            "hpcint",
            "/bin/sh",
        ]
    ]


def test_lsf_interactive_submission_sets_explicit_core_and_host_defaults() -> None:
    process = FakeProcess(stdout=[b"Job <99> is submitted to queue <hpcint>.\n"])
    commands: list[list[str]] = []
    scheduler = LSFScheduler(
        FakeRunner(),
        lambda command: commands.append(command) or process,
        interactive_application_profile="qrsh",
        interactive_submission_environment={
            "ESUB_BYPASS": "1",
            "ESUB_QUIET": "1",
            "LSF_QRSH": "true",
        },
        interactive_export_environment=("TERM", "LSF_QRSH"),
    )

    scheduler.submit_interactive(
        JobSpec(
            command=(_COMMAND, "54321"),
            name="ezhpcy-worker",
            queue="hpcint",
        )
    )

    assert commands == [
        [
            "env",
            "ESUB_BYPASS=1",
            "ESUB_QUIET=1",
            "LSF_QRSH=true",
            "bsub",
            "-Is",
            "-app",
            "qrsh",
            "-n",
            "1",
            "-R",
            "span[hosts=1]",
            "-env",
            "TERM,LSF_QRSH",
            "-J",
            "ezhpcy-worker",
            "-q",
            "hpcint",
            "/bin/sh",
        ]
    ]


def test_lsf_interactive_submission_can_use_site_wrapper() -> None:
    process = FakeProcess(stdout=[b"Job <100> is submitted to queue <gpua100i>.\n"])
    commands: list[list[str]] = []
    scheduler = LSFScheduler(
        FakeRunner(),
        lambda command: commands.append(command) or process,
        interactive_application_profile="qrsh",
        interactive_submission_command=("/lsf/local/bin/a100sh",),
        interactive_submission_environment={"LSF_QRSH": "true"},
    )

    job = scheduler.submit_interactive(
        JobSpec(
            command=(_COMMAND, "54321"),
            queue="gpua100i",
            gpus=2,
            working_directory=PurePosixPath("/home/user/worker"),
        )
    )
    job.start_command()

    assert commands == [["/lsf/local/bin/a100sh"]]
    assert process.sent == [f"cd /home/user/worker && exec {_COMMAND} 54321\n"]


def test_lsf_interactive_worker_command_round_trips_shell_sensitive_arguments() -> None:
    process = FakeProcess(stdout=[b"Job <42> is submitted to queue <hpcint>.\n"])
    scheduler = LSFScheduler(FakeRunner(), lambda _command: process)
    arguments = (
        "/path with spaces/sshd",
        "single'quote",
        'double"quote',
        "$HOME; echo unsafe",
        "AuthorizedKeysCommand=/bin/echo ssh-ed25519 KEY+/=",
    )

    job = scheduler.submit_interactive(
        JobSpec(
            command=arguments,
            environment={"WORKER_VALUE": "space and 'quotes'"},
        )
    )

    assert process.sent == []

    job.start_command()

    assert len(process.sent) == 1
    serialized = process.sent[0]
    assert isinstance(serialized, str)
    assert shlex.split(serialized) == [
        "exec",
        "env",
        "WORKER_VALUE=space and 'quotes'",
        *arguments,
    ]


def test_lsf_interactive_submission_closes_process_on_early_exit() -> None:
    process = FakeProcess(stderr=[b"submission rejected\n"], exit_status=2)
    scheduler = LSFScheduler(FakeRunner(), lambda _command: process)

    with pytest.raises(SchedulerCommandError, match="submission rejected"):
        scheduler.submit_interactive(JobSpec(command=("true",)))

    assert process.closed


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        ("PEND", JobState.PENDING),
        ("WAIT", JobState.PENDING),
        ("RUN", JobState.RUNNING),
        ("PSUSP", JobState.SUSPENDED),
        ("USUSP", JobState.SUSPENDED),
        ("SSUSP", JobState.SUSPENDED),
        ("DONE", JobState.SUCCEEDED),
        ("POST_DONE", JobState.SUCCEEDED),
        ("EXIT", JobState.FAILED),
        ("POST_ERR", JobState.FAILED),
        ("ZOMBI", JobState.FAILED),
        ("UNKWN", JobState.UNKNOWN),
        ("FUTURE_STATE", JobState.UNKNOWN),
    ],
)
def test_lsf_states_are_normalized(raw_state: str, expected: JobState) -> None:
    runner = FakeRunner(f"42|{raw_state}|-|-")

    info = LSFScheduler(runner).inspect("42")

    assert info.state is expected
    assert info.raw_state == raw_state


def test_lsf_inspection_returns_execution_host_and_exit_code() -> None:
    runner = FakeRunner("42|DONE|4*node1:4*node1:2*node2|0\n")

    info = LSFScheduler(runner).inspect("42")

    assert info.execution_hosts == ("node1", "node2")
    assert info.primary_host == "node1"
    assert info.exit_code == 0
    assert info.state.is_terminal
    assert runner.commands == [
        [
            "bjobs",
            "-a",
            "-noheader",
            "-o",
            "jobid stat exec_host exit_code delimiter='|'",
            "42",
        ]
    ]


def test_lsf_missing_job_is_distinct_from_command_failure() -> None:
    runner = FakeRunner()
    runner.error = RuntimeError("Remote command failed (255): Job <42> is not found")

    with pytest.raises(JobNotFoundError, match="'42'"):
        LSFScheduler(runner).inspect("42")


def test_lsf_other_command_failure_is_wrapped() -> None:
    runner = FakeRunner()
    runner.error = RuntimeError("LSF daemon is unavailable")

    with pytest.raises(SchedulerCommandError, match="inspect job"):
        LSFScheduler(runner).inspect("42")


def test_lsf_rejects_unparseable_output() -> None:
    with pytest.raises(SchedulerOutputError, match="four bjobs fields"):
        LSFScheduler(FakeRunner("42 RUN node1")).inspect("42")


def test_lsf_cancellation_targets_exact_job_id() -> None:
    runner = FakeRunner("Job <42> is being terminated\n")

    LSFScheduler(runner).cancel("42")

    assert runner.commands == [["bkill", "42"]]


def test_pbs_submission_builds_portable_job_spec() -> None:
    runner = FakeRunner("31415.hnode41\n")
    scheduler = PBSScheduler(runner)
    spec = JobSpec(
        command=(_COMMAND, "34321"),
        name="ezhpcy-worker",
        time_limit=timedelta(hours=1, seconds=1),
        memory_bytes=1024 * _MEBIBYTE,
        cores=4,
        gpus=2,
        exclusive=True,
        queue="workq",
        working_directory=PurePosixPath("/home/user"),
        stdout_path=PurePosixPath("/home/user/worker.out"),
        stderr_path=PurePosixPath("/home/user/worker.err"),
        environment={"EZHPCY_PROFILE": "default"},
    )

    assert scheduler.submit(spec) == "31415.hnode41"
    assert runner.commands == [
        [
            "qsub",
            "-N",
            "ezhpcy-worker",
            "-q",
            "workq",
            "-l",
            "select=1:ncpus=4:mem=1073741824b:ngpus=2",
            "-l",
            "place=exclhost",
            "-l",
            "walltime=01:00:01",
            "-o",
            "/home/user/worker.out",
            "-e",
            "/home/user/worker.err",
            "--",
            "bash",
            "-lc",
            f"cd /home/user && exec env EZHPCY_PROFILE=default {_COMMAND} 34321",
        ]
    ]


def test_pbs_interactive_submission_uses_site_queue_and_starts_command() -> None:
    process = FakeProcess(stdout=[b"qsub: waiting for job 690874.hnode41 to start\r\n"])
    commands: list[list[str]] = []
    scheduler = PBSScheduler(
        FakeRunner(),
        lambda command: commands.append(command) or process,
    )
    spec = JobSpec(
        command=(_COMMAND, "54321"),
        name="ezhpcy-worker",
        memory_bytes=256 * 1024 * _MEBIBYTE,
        cores=32,
        queue="workq",
        working_directory=PurePosixPath("/home/user"),
        environment={"EZHPCY_PROFILE": "interactive"},
    )

    job = scheduler.submit_interactive(spec)
    job.start_command()
    job.start_command()

    assert job.job_id == "690874.hnode41"
    assert job.submission_command == tuple(commands[0])
    assert commands == [
        [
            "qsub",
            "-I",
            "-N",
            "ezhpcy-worker",
            "-q",
            "workq",
            "-l",
            "select=1:ncpus=32:mem=274877906944b",
        ]
    ]
    assert process.sent == [
        f"cd /home/user && exec env EZHPCY_PROFILE=interactive {_COMMAND} 54321\n"
    ]


def test_pbs_interactive_submission_sets_ezhpcy_default_core_count() -> None:
    process = FakeProcess(stdout=[b"qsub: waiting for job 42.server to start\n"])
    commands: list[list[str]] = []
    scheduler = PBSScheduler(
        FakeRunner(),
        lambda command: commands.append(command) or process,
        command_directory=PurePosixPath("/opt/pbspro/bin"),
    )

    scheduler.submit_interactive(
        JobSpec(command=("true",), name="ezhpcy-worker", queue="workq")
    )

    assert commands == [
        [
            "/opt/pbspro/bin/qsub",
            "-I",
            "-N",
            "ezhpcy-worker",
            "-q",
            "workq",
            "-l",
            "select=1:ncpus=1",
        ]
    ]


def test_pbs_interactive_submission_can_use_site_wrapper() -> None:
    process = FakeProcess(stdout=[b"qsub: waiting for job 100.server to start\n"])
    commands: list[list[str]] = []
    scheduler = PBSScheduler(
        FakeRunner(),
        lambda command: commands.append(command) or process,
        interactive_submission_command=("/site/bin/interactive-pbs", "--gpu"),
    )

    job = scheduler.submit_interactive(
        JobSpec(command=(_COMMAND, "54321"), queue="gpuq", gpus=1)
    )
    job.start_command()

    assert commands == [["/site/bin/interactive-pbs", "--gpu"]]
    assert process.sent == [f"exec {_COMMAND} 54321\n"]


def test_pbs_inspection_parses_json_state_hosts_and_exit_status() -> None:
    runner = FakeRunner(
        json.dumps(
            {
                "Jobs": {
                    "690874.hnode41.hpccluster.dtu.dk": {
                        "job_state": "R",
                        "exec_host": "node1/0*2+node1/1+node2/0",
                    }
                }
            }
        )
    )

    info = PBSScheduler(runner).inspect("690874.hnode41")

    assert info.state is JobState.RUNNING
    assert info.execution_hosts == ("node1", "node2")
    assert runner.commands == [["qstat", "-f", "-F", "json", "690874.hnode41"]]

    runner.output = json.dumps(
        {"Jobs": {"690874.hnode41": {"job_state": "F", "Exit_status": 0}}}
    )
    assert PBSScheduler(runner).inspect("690874.hnode41").state is JobState.SUCCEEDED


def test_pbs_missing_job_and_cancellation_use_exact_job_id() -> None:
    runner = FakeRunner()
    runner.error = RuntimeError("qstat: Unknown Job Id 42.server")
    with pytest.raises(JobNotFoundError, match="'42.server'"):
        PBSScheduler(runner).inspect("42.server")

    assert runner.commands == [
        ["qstat", "-f", "-F", "json", "42.server"],
        ["qstat", "-x", "-f", "-F", "json", "42.server"],
    ]

    runner.error = None
    PBSScheduler(runner).cancel("42.server")
    assert runner.commands[-1] == ["qdel", "42.server"]


def test_pbs_inspection_falls_back_to_job_history() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str]) -> str:
        commands.append(command)
        if "-x" not in command:
            raise RuntimeError("qstat: Unknown Job Id 42.server")
        return json.dumps({"Jobs": {"42.server": {"job_state": "F", "Exit_status": 0}}})

    info = PBSScheduler(runner).inspect("42.server")

    assert info.state is JobState.SUCCEEDED
    assert commands == [
        ["qstat", "-f", "-F", "json", "42.server"],
        ["qstat", "-x", "-f", "-F", "json", "42.server"],
    ]


def test_job_spec_validates_commands_time_limits_and_environment() -> None:
    with pytest.raises(ValueError, match="command"):
        JobSpec(command=())
    with pytest.raises(ValueError, match="time_limit"):
        JobSpec(command=("true",), time_limit=timedelta(0))
    with pytest.raises(ValueError, match="memory_bytes"):
        JobSpec(command=("true",), memory_bytes=0)
    with pytest.raises(ValueError, match="cores"):
        JobSpec(command=("true",), cores=0)
    with pytest.raises(ValueError, match="gpus"):
        JobSpec(command=("true",), gpus=-1)
    with pytest.raises(ValueError, match="environment"):
        JobSpec(command=("true",), environment={"NOT-VALID": "value"})
