"""Tests for the async schedulers in `ezhpcy.scheduler`."""

import asyncio
import json
import shlex
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any

import asyncssh
import pytest

from ezhpcy import ssh
from ezhpcy.scheduler import create_scheduler
from ezhpcy.scheduler.base import JobSpec, JobState, Scheduler, SchedulerError
from ezhpcy.scheduler.lsf import LSFScheduler
from ezhpcy.scheduler.pbs import PBSScheduler
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.ssh import RemoteHost
from ezhpcy.types import (
    ConnectionInfo,
    LSFConfig,
    PBSConfig,
    ResolvedConfig,
    ResolvedSchedulerConfig,
    SubmissionMode,
)
from tests.support.sshserver import Handler, Invocation, SSHTestServer, reply

_MEBIBYTE = 1024**2
_COMMAND = "/home/user/bin/worker"
# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5


# Fakes


class FakeStdout:
    """Returns one chunk per read, then end-of-file, or blocks if not `eof`."""

    def __init__(self, chunks: tuple[bytes, ...], *, eof: bool) -> None:
        self._chunks = list(chunks)
        self._eof = eof

    async def read(self, n: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if not self._eof:
            await asyncio.Event().wait()
        return b""


class FakeStdin:
    def __init__(self) -> None:
        self.written = bytearray()
        self.broken = False

    def write(self, data: bytes) -> None:
        if self.broken:
            raise BrokenPipeError("channel is closed")
        self.written.extend(data)

    async def drain(self) -> None:
        pass


class FakeProcess:
    """
    Stands in for `RemoteHost.start_login_shell`'s process: prints `output`,
    then exits with `exit_status`, or keeps running if it's `None`.
    """

    def __init__(self, *output: bytes, exit_status: int | None = None) -> None:
        self.stdout = FakeStdout(output, eof=exit_status is not None)
        self.stdin = FakeStdin()
        self.exit_status = exit_status
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class FakeHost:
    """
    Stands in for `RemoteHost`. `run_login_shell` returns `output` (or raises
    `error`); `start_login_shell` returns `process`. Both record the commands.
    """

    def __init__(self, output: str = "", process: FakeProcess | None = None) -> None:
        self.output = output
        self.error: Exception | None = None
        self.process = process
        self.commands: list[list[str]] = []
        self.stdin: list[str | None] = []
        self.started: list[list[str]] = []

    async def run_login_shell(
        self, args: list[str], *, stdin: str | None = None
    ) -> str:
        self.commands.append(args)
        self.stdin.append(stdin)
        if self.error is not None:
            raise self.error
        return self.output

    async def start_login_shell(self, args: list[str]) -> FakeProcess:
        self.started.append(args)
        assert self.process is not None, "no interactive submission expected"
        return self.process


def _failed(stderr: str) -> asyncssh.ProcessError:
    """A remote command's failure, as `RemoteHost.run` raises it."""
    return asyncssh.ProcessError(None, "command", None, 255, None, 255, "", stderr)


def _lsf(host: Any, **lsf: Any) -> LSFScheduler:
    return LSFScheduler(host, LSFConfig(**lsf))


def _pbs(host: Any, **pbs: Any) -> PBSScheduler:
    return PBSScheduler(host, PBSConfig(**pbs))


# Shared behaviour of both adapters


@dataclass(frozen=True)
class Adapter:
    scheduler: type[LSFScheduler | PBSScheduler]
    config: LSFConfig | PBSConfig
    # The interactive submission command, and its output naming `job_id`.
    submit_command: str
    submitted: str
    job_id: str

    def create(self, host: Any, **options: Any) -> Scheduler:
        return self.scheduler(host, self.config, **options)


_ADAPTERS = [
    pytest.param(
        Adapter(
            LSFScheduler,
            LSFConfig(),
            "bsub",
            "Job <2718> is submitted to queue <hpcint>.\r\n",
            "2718",
        ),
        id="lsf",
    ),
    pytest.param(
        Adapter(
            PBSScheduler,
            PBSConfig(),
            "qsub",
            "qsub: waiting for job 690874.hnode41 to start\r\n",
            "690874.hnode41",
        ),
        id="pbs",
    ),
]


@pytest.fixture(params=_ADAPTERS)
def adapter(request: pytest.FixtureRequest) -> Adapter:
    return request.param


async def test_interactive_submission_waits_to_start_the_command(adapter: Adapter):
    process = FakeProcess(adapter.submitted.encode())
    host = FakeHost(process=process)

    job = await adapter.create(host).submit_interactive(
        JobSpec(command=(_COMMAND, "54321")), startup_timeout=WAIT_SECONDS
    )

    assert job.job_id == adapter.job_id
    assert job.process is process
    assert job.submission_command == tuple(host.started[0])
    assert process.stdin.written == b""
    assert not process.closed

    await job.start_command()

    assert process.stdin.written == f"exec {_COMMAND} 54321\n".encode()


async def test_interactive_submission_finds_a_job_id_split_across_reads(
    adapter: Adapter,
):
    submitted = adapter.submitted.encode()
    process = FakeProcess(b"<<Submitting>>\n", submitted[:15], submitted[15:])

    job = await adapter.create(FakeHost(process=process)).submit_interactive(
        JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
    )

    assert job.job_id == adapter.job_id


async def test_interactive_submission_can_use_site_wrapper(adapter: Adapter):
    process = FakeProcess(adapter.submitted.encode())
    host = FakeHost(process=process)
    scheduler = adapter.create(
        host, interactive_submission_command=["/site/bin/interactive", "--gpu"]
    )

    job = await scheduler.submit_interactive(
        JobSpec(
            command=(_COMMAND, "54321"),
            queue="gpuq",
            gpus=1,
            working_directory=PurePosixPath("/home/user/worker"),
        ),
        startup_timeout=WAIT_SECONDS,
    )
    await job.start_command()

    assert host.started == [["/site/bin/interactive", "--gpu"]]
    assert process.stdin.written == (
        f"cd /home/user/worker && exec {_COMMAND} 54321\n".encode()
    )


async def test_interactive_worker_command_round_trips_shell_sensitive_arguments(
    adapter: Adapter,
):
    process = FakeProcess(adapter.submitted.encode())
    arguments = (
        "/path with spaces/sshd",
        "single'quote",
        'double"quote',
        "$HOME; echo unsafe",
        "AuthorizedKeysCommand=/bin/echo ssh-ed25519 KEY+/=",
    )

    job = await adapter.create(FakeHost(process=process)).submit_interactive(
        JobSpec(command=arguments), startup_timeout=WAIT_SECONDS
    )
    await job.start_command()

    assert shlex.split(process.stdin.written.decode()) == ["exec", *arguments]


async def test_interactive_submission_closes_the_process_on_early_exit(
    adapter: Adapter,
):
    process = FakeProcess(b"submission rejected\n", exit_status=2)

    with pytest.raises(
        SchedulerError,
        match=rf"^{adapter.scheduler.scheduler_type} interactive submission exited "
        r"before returning a job ID \(2\): submission rejected$",
    ):
        await adapter.create(FakeHost(process=process)).submit_interactive(
            JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
        )

    assert process.closed


async def test_interactive_submission_closes_the_process_on_timeout(adapter: Adapter):
    process = FakeProcess(b"<<Waiting for dispatch ...>>\n")

    with pytest.raises(
        SchedulerError,
        match=rf"^{adapter.scheduler.scheduler_type} interactive submission did not "
        r"return a job ID within 0\.05 seconds: <<Waiting for dispatch \.\.\.>>$",
    ):
        await adapter.create(FakeHost(process=process)).submit_interactive(
            JobSpec(command=("true",)), startup_timeout=0.05
        )

    assert process.closed


async def test_interactive_submission_closes_the_process_when_cancelled(
    adapter: Adapter,
):
    process = FakeProcess()
    submission = asyncio.create_task(
        adapter.create(FakeHost(process=process)).submit_interactive(
            JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
        )
    )
    await asyncio.sleep(0)

    submission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submission

    assert process.closed


async def test_interactive_submission_reports_a_failed_start(adapter: Adapter):
    class FailingHost(FakeHost):
        async def start_login_shell(self, args: list[str]) -> FakeProcess:
            raise asyncssh.ChannelOpenError(asyncssh.OPEN_CONNECT_FAILED, "no session")

    with pytest.raises(
        SchedulerError,
        match=f"^{adapter.scheduler.scheduler_type} failed to start interactive "
        "submission: no session$",
    ):
        await adapter.create(FailingHost()).submit_interactive(
            JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
        )


async def test_start_command_reports_a_closed_shell(adapter: Adapter):
    process = FakeProcess(adapter.submitted.encode())
    job = await adapter.create(FakeHost(process=process)).submit_interactive(
        JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
    )
    process.stdin.broken = True

    with pytest.raises(
        SchedulerError,
        match=r"^interactive shell did not accept the worker command: "
        r"channel is closed$",
    ):
        await job.start_command()


async def test_drain_passes_on_output_until_the_process_ends(adapter: Adapter):
    process = FakeProcess(
        adapter.submitted.encode(), b"worker log\n", b"more\n", exit_status=0
    )
    job = await adapter.create(FakeHost(process=process)).submit_interactive(
        JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
    )
    output: list[bytes] = []

    await asyncio.wait_for(job.drain(output.append), WAIT_SECONDS)

    assert output == [adapter.submitted.encode(), b"worker log\n", b"more\n"]


async def test_drain_first_passes_on_what_was_read_for_the_job_id(adapter: Adapter):
    submitted = adapter.submitted.encode()
    process = FakeProcess(
        b"Welcome to the cluster\n",
        submitted + b"<<Waiting for dispatch ...>>\n",
        b"worker log\n",
        exit_status=0,
    )
    job = await adapter.create(FakeHost(process=process)).submit_interactive(
        JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
    )
    output: list[bytes] = []

    await asyncio.wait_for(job.drain(output.append), WAIT_SECONDS)

    # Nothing is lost: not the banner before the ID, nor what followed it.
    assert b"".join(output) == (
        b"Welcome to the cluster\n"
        + submitted
        + b"<<Waiting for dispatch ...>>\nworker log\n"
    )


async def test_command_failures_carry_the_reason(adapter: Adapter):
    host = FakeHost()
    scheduler = adapter.create(host)

    host.error = _failed("scheduler daemon is unavailable")
    with pytest.raises(
        SchedulerError,
        match=f"^{adapter.scheduler.scheduler_type} failed to inspect job",
    ) as raised:
        await scheduler.inspect(adapter.job_id)
    assert raised.value.__cause__ is host.error

    host.error = asyncssh.ConnectionLost("connection lost")
    with pytest.raises(
        SchedulerError,
        match=f"^{adapter.scheduler.scheduler_type} failed to cancel job "
        f"'{adapter.job_id}': connection lost$",
    ):
        await scheduler.cancel(adapter.job_id)
    with pytest.raises(SchedulerError, match="failed to submit a job"):
        await scheduler.submit(JobSpec(command=("true",)))


async def test_programming_errors_are_not_reported_as_scheduler_failures(
    adapter: Adapter,
):
    host = FakeHost()
    host.error = TypeError("a bug")

    with pytest.raises(TypeError):
        await adapter.create(host).cancel(adapter.job_id)


# LSF


async def test_lsf_submission_builds_portable_job_spec():
    host = FakeHost("Job <31415> is submitted to queue <normal>.\n")
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
    )

    assert await _lsf(host).submit(spec) == "31415"
    assert host.commands == [
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
        ]
    ]
    assert host.stdin == [f"#!/usr/bin/env bash\nexec {_COMMAND} 34321\n"]


async def test_lsf_submission_keeps_the_command_quoted():
    host = FakeHost("Job <31415> is submitted to queue <normal>.\n")
    spec = JobSpec(command=("bash", "-c", "echo \"$1\"\necho 'done'", "two words"))

    await _lsf(host).submit(spec)

    [script] = host.stdin
    assert script is not None
    shebang, _, body = script.partition("\n")
    assert shebang == "#!/usr/bin/env bash"
    assert shlex.split(body) == ["exec", *spec.command]


async def test_lsf_submission_without_a_job_id_is_an_error():
    host = FakeHost("Request aborted by esub.\n")

    with pytest.raises(SchedulerError, match="Request aborted by esub"):
        await _lsf(host).submit(JobSpec(command=("true",)))


async def test_lsf_interactive_submission_uses_site_profile():
    host = FakeHost(
        process=FakeProcess(b"Job <2718> is submitted to queue <hpcint>.\r\n")
    )
    scheduler = _lsf(
        host,
        application_profile="qrsh",
        submission_environment={
            "ESUB_BYPASS": "1",
            "ESUB_QUIET": "1",
            "LSF_QRSH": "true",
        },
        export_environment=["TERM", "LSF_QRSH"],
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

    await scheduler.submit_interactive(spec, startup_timeout=WAIT_SECONDS)

    assert host.started == [
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


async def test_lsf_interactive_submission_sets_explicit_core_and_host_defaults():
    host = FakeHost(process=FakeProcess(b"Job <99> is submitted to queue <hpcint>.\n"))

    await _lsf(host).submit_interactive(
        JobSpec(command=(_COMMAND, "54321"), name="ezhpcy-worker", queue="hpcint"),
        startup_timeout=WAIT_SECONDS,
    )

    assert host.started == [
        [
            "bsub",
            "-Is",
            "-n",
            "1",
            "-R",
            "span[hosts=1]",
            "-J",
            "ezhpcy-worker",
            "-q",
            "hpcint",
            "/bin/sh",
        ]
    ]


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        ("PEND", JobState.PENDING),
        ("WAIT", JobState.PENDING),
        ("PROV", JobState.PENDING),
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
async def test_lsf_states_are_normalized(raw_state: str, expected: JobState):
    info = await _lsf(FakeHost(f"42|{raw_state}|-|-")).inspect("42")

    assert info.state is expected
    assert info.raw_state == raw_state
    assert info.host is None
    assert info.exit_code is None


async def test_lsf_inspection_returns_execution_host_and_exit_code():
    host = FakeHost("42|DONE|4*node1:4*node1:2*node2|0\n")

    info = await _lsf(host).inspect("42")

    assert info.host == "node1"
    assert info.exit_code == 0
    assert info.state.is_terminal
    assert host.commands == [
        [
            "bjobs",
            "-a",
            "-noheader",
            "-o",
            "jobid stat exec_host exit_code delimiter='|'",
            "42",
        ]
    ]


@pytest.mark.parametrize(
    "output", ["42 RUN node1", "", "42|RUN|-|-\n43|RUN|-|-\n", "42|EXIT|-|oops"]
)
async def test_lsf_rejects_unparseable_output(output: str):
    with pytest.raises(SchedulerError, match="could not parse bjobs output"):
        await _lsf(FakeHost(output)).inspect("42")


async def test_lsf_cancellation_targets_exact_job_id():
    host = FakeHost("Job <42> is being terminated\n")

    await _lsf(host).cancel("42")

    assert host.commands == [["bkill", "42"]]


# PBS


async def test_pbs_submission_builds_portable_job_spec():
    host = FakeHost("31415.hnode41\n")
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
    )

    assert await _pbs(host).submit(spec) == "31415.hnode41"
    assert host.commands == [
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
            f"cd /home/user && exec {_COMMAND} 34321",
        ]
    ]


async def test_pbs_submission_without_working_directory_runs_the_command_directly():
    host = FakeHost("7.server\n")

    await _pbs(host).submit(JobSpec(command=(_COMMAND, "34321"), name="ezhpcy-worker"))

    assert host.commands == [
        [
            "qsub",
            "-N",
            "ezhpcy-worker",
            "-l",
            "select=1:ncpus=1",
            "--",
            _COMMAND,
            "34321",
        ]
    ]


async def test_pbs_submission_without_a_job_id_is_an_error():
    with pytest.raises(SchedulerError, match="queue is disabled"):
        await _pbs(FakeHost("qsub: queue is disabled\n")).submit(
            JobSpec(command=("true",))
        )


async def test_pbs_interactive_submission_uses_site_queue():
    process = FakeProcess(b"qsub: waiting for job 690874.hnode41 to start\r\n")
    host = FakeHost(process=process)
    spec = JobSpec(
        command=(_COMMAND, "54321"),
        name="ezhpcy-worker",
        memory_bytes=256 * 1024 * _MEBIBYTE,
        cores=32,
        queue="workq",
        working_directory=PurePosixPath("/home/user"),
    )

    job = await _pbs(host).submit_interactive(spec, startup_timeout=WAIT_SECONDS)
    await job.start_command()

    assert host.started == [
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
    assert process.stdin.written == f"cd /home/user && exec {_COMMAND} 54321\n".encode()


async def test_pbs_uses_the_configured_command_directory():
    host = FakeHost(
        json.dumps({"Jobs": {"42.server": {"job_state": "R"}}}),
        process=FakeProcess(b"qsub: waiting for job 42.server to start\n"),
    )
    scheduler = _pbs(host, command_directory=PurePosixPath("/opt/pbspro/bin"))

    await scheduler.submit_interactive(
        JobSpec(command=("true",), name="ezhpcy-worker", queue="workq"),
        startup_timeout=WAIT_SECONDS,
    )
    await scheduler.inspect("42.server")
    await scheduler.cancel("42.server")

    assert host.started == [
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
    assert host.commands == [
        ["/opt/pbspro/bin/qstat", "-f", "-F", "json", "42.server"],
        ["/opt/pbspro/bin/qdel", "42.server"],
    ]


async def test_pbs_inspection_parses_json_state_host_and_exit_status():
    host = FakeHost(
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
    scheduler = _pbs(host)

    info = await scheduler.inspect("690874.hnode41")

    assert info.state is JobState.RUNNING
    assert info.host == "node1"
    assert host.commands == [["qstat", "-f", "-F", "json", "690874.hnode41"]]

    host.output = json.dumps(
        {"Jobs": {"690874.hnode41": {"job_state": "F", "Exit_status": 0}}}
    )
    info = await scheduler.inspect("690874.hnode41")
    assert info.state is JobState.SUCCEEDED
    assert info.host is None


@pytest.mark.parametrize(
    ("raw_state", "exit_status", "expected"),
    [
        ("Q", None, JobState.PENDING),
        ("R", None, JobState.RUNNING),
        ("H", None, JobState.SUSPENDED),
        ("F", 0, JobState.SUCCEEDED),
        ("F", 271, JobState.FAILED),
        ("X", None, JobState.FAILED),
        ("Z", None, JobState.UNKNOWN),
    ],
)
async def test_pbs_states_are_normalized(
    raw_state: str, exit_status: int | None, expected: JobState
):
    record: dict[str, object] = {"job_state": raw_state}
    if exit_status is not None:
        record["Exit_status"] = exit_status
    host = FakeHost(json.dumps({"Jobs": {"42.server": record}}))

    info = await _pbs(host).inspect("42.server")

    assert info.state is expected
    assert info.exit_code == exit_status


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        json.dumps({"Jobs": []}),
        json.dumps({"Jobs": {}}),
        json.dumps({"Jobs": {"1.server": {}, "2.server": {}}}),
        json.dumps({"Jobs": {"42.server": {"job_state": "F", "Exit_status": "x"}}}),
        json.dumps({"Jobs": {"42.server": "R"}}),
    ],
)
async def test_pbs_rejects_unparseable_output(output: str):
    with pytest.raises(SchedulerError, match="could not parse PBS qstat output"):
        await _pbs(FakeHost(output)).inspect("42.server")


async def test_pbs_shortens_long_unparseable_output_in_errors():
    with pytest.raises(SchedulerError) as raised:
        await _pbs(FakeHost("x" * 2000)).inspect("42.server")

    assert f"'{'x' * 1000}...'" in str(raised.value)


async def test_pbs_inspection_falls_back_to_job_history():
    class HistoryHost(FakeHost):
        async def run_login_shell(
            self, args: list[str], *, stdin: str | None = None
        ) -> str:
            output = await super().run_login_shell(args, stdin=stdin)
            if "-x" not in args:
                raise _failed("qstat: Unknown Job Id 42.server")
            return output

    host = HistoryHost(
        json.dumps({"Jobs": {"42.server": {"job_state": "F", "Exit_status": 0}}})
    )

    info = await _pbs(host).inspect("42.server")

    assert info.state is JobState.SUCCEEDED
    assert host.commands == [
        ["qstat", "-f", "-F", "json", "42.server"],
        ["qstat", "-x", "-f", "-F", "json", "42.server"],
    ]


async def test_pbs_job_missing_from_the_history_has_ended():
    host = FakeHost()
    host.error = _failed("qstat: Unknown Job Id 42.server")

    info = await _pbs(host).inspect("42.server")

    assert info.state is JobState.ENDED
    assert info.state.is_terminal
    assert info.exit_code is None
    assert len(host.commands) == 2


async def test_pbs_job_has_ended_when_the_server_keeps_no_history():
    # What DTU's server answers once a job has ended: `qstat -x` exits 0, but
    # lists no jobs (and says on stderr that it keeps no job history).
    class NoHistoryHost(FakeHost):
        async def run_login_shell(
            self, args: list[str], *, stdin: str | None = None
        ) -> str:
            output = await super().run_login_shell(args, stdin=stdin)
            if "-x" not in args:
                raise _failed("qstat: Unknown Job Id 42.server")
            return output

    host = NoHistoryHost(json.dumps({"timestamp": 1792065543, "pbs_version": "20.0.0"}))

    info = await _pbs(host).inspect("42.server")

    assert info.state is JobState.ENDED


async def test_pbs_history_query_failure_is_still_an_error():
    class FailingHistoryHost(FakeHost):
        async def run_login_shell(
            self, args: list[str], *, stdin: str | None = None
        ) -> str:
            await super().run_login_shell(args, stdin=stdin)
            if "-x" not in args:
                raise _failed("qstat: Unknown Job Id 42.server")
            raise _failed("qstat: cannot connect to server hnode41")

    with pytest.raises(
        SchedulerError, match=r"^PBS failed to inspect historical job '42\.server'"
    ):
        await _pbs(FailingHistoryHost()).inspect("42.server")


# create_scheduler


def _resolved(**sections: Any) -> ResolvedConfig:
    return ResolvedConfig(
        connection=ConnectionInfo(user="alice", host="login.example.com"), **sections
    )


async def test_create_scheduler_configures_lsf_from_the_profile():
    host = FakeHost(process=FakeProcess(b"Job <1> is submitted to queue <q>.\n"))
    scheduler = create_scheduler(
        _resolved(
            scheduler=ResolvedSchedulerConfig(
                type=SchedulerType.LSF, submission_mode=SubmissionMode.INTERACTIVE
            ),
            lsf=LSFConfig(application_profile="qrsh"),
        ),
        host,
    )

    await scheduler.submit_interactive(
        JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
    )

    assert isinstance(scheduler, LSFScheduler)
    assert host.started[0][:4] == ["bsub", "-Is", "-app", "qrsh"]


async def test_create_scheduler_configures_pbs_from_the_profile():
    host = FakeHost(process=FakeProcess(b"qsub: waiting for job 1.server to start\n"))
    scheduler = create_scheduler(
        _resolved(
            scheduler=ResolvedSchedulerConfig(
                type=SchedulerType.PBS,
                submission_mode=SubmissionMode.INTERACTIVE,
                interactive_submission_command=["linuxsh"],
            ),
            pbs=PBSConfig(command_directory=PurePosixPath("/opt/pbspro/bin")),
        ),
        host,
    )

    await scheduler.submit_interactive(
        JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
    )
    await scheduler.cancel("1.server")

    assert isinstance(scheduler, PBSScheduler)
    assert host.started == [["linuxsh"]]
    assert host.commands == [["/opt/pbspro/bin/qdel", "1.server"]]


# Against the in-process SSH server


async def _connect(server: SSHTestServer) -> RemoteHost:
    conn_info = ConnectionInfo.model_construct(user=server.user, host=server.host)
    return await ssh.connect(
        conn_info,
        port=server.port,
        client_keys=[server.client_key],
        agent_path=None,
        known_hosts=server.known_hosts,
    )


def _login_shell(server: SSHTestServer, **fakes: Handler) -> None:
    """
    Serve `bash -lc '<command>'` with the fake named after the command's first
    word, which sees the command's own words.
    """

    async def dispatch(invocation: Invocation) -> int | None:
        argv = shlex.split(invocation.argv[2])
        result = fakes[argv[0]](Invocation(argv, invocation.process))
        return await result if isinstance(result, Awaitable) else result

    server.command("bash -lc", dispatch)


async def test_lsf_on_the_login_node(ssh_server: SSHTestServer):
    scripts: list[str] = []

    async def bsub(invocation: Invocation) -> None:
        scripts.append(await invocation.stdin.read())
        invocation.stdout.write("Job <7> is submitted to queue <normal>.\n")

    _login_shell(
        ssh_server,
        bsub=bsub,
        bjobs=reply(stdout="7|RUN|node1|-\n"),
        bkill=reply(stdout="Job <7> is being terminated\n"),
    )

    async with await _connect(ssh_server) as host:
        scheduler = _lsf(host)
        job_id = await scheduler.submit(JobSpec(command=(_COMMAND, "two words")))
        info = await scheduler.inspect(job_id)
        await scheduler.cancel(job_id)

    assert info.state is JobState.RUNNING
    assert info.host == "node1"
    assert scripts == [f"#!/usr/bin/env bash\nexec {_COMMAND} 'two words'\n"]
    commands = [shlex.split(argv[2]) for argv in ssh_server.commands_run]
    assert commands[0][0] == "bsub"
    assert commands[1][0] == "bjobs"
    assert commands[2] == ["bkill", "7"]


async def test_lsf_command_failure_on_the_login_node_carries_stderr(
    ssh_server: SSHTestServer,
):
    _login_shell(
        ssh_server, bjobs=reply(stderr="Job <7> is not found\n", exit_status=255)
    )

    async with await _connect(ssh_server) as host:
        with pytest.raises(
            SchedulerError,
            match=r"^LSF failed to inspect job '7': Remote command failed \(255\): "
            "Job <7> is not found$",
        ):
            await _lsf(host).inspect("7")


async def test_pbs_on_the_login_node(ssh_server: SSHTestServer):
    def qstat(invocation: Invocation) -> int:
        if "-x" not in invocation.argv:
            invocation.stderr.write("qstat: Unknown Job Id 9.server\n")
            return 153
        invocation.stdout.write(
            json.dumps({"Jobs": {"9.server": {"job_state": "F", "Exit_status": 0}}})
        )
        return 0

    _login_shell(ssh_server, qsub=reply(stdout="9.server\n"), qstat=qstat)

    async with await _connect(ssh_server) as host:
        scheduler = _pbs(host)
        job_id = await scheduler.submit(JobSpec(command=(_COMMAND,)))
        info = await scheduler.inspect(job_id)

    assert job_id == "9.server"
    assert info.state is JobState.SUCCEEDED


async def test_pbs_without_job_history_on_the_login_node(ssh_server: SSHTestServer):
    # As DTU's server answered when probed (see FINDINGS.md).
    envelope = '{\n    "timestamp":1792065543,\n    "pbs_version":"20.0.0"\n}'

    def qstat(invocation: Invocation) -> int:
        job_id = invocation.argv[-1]
        if "-x" in invocation.argv:
            invocation.stderr.write(
                f"qstat: PBS is not configured to maintain job history {job_id}\n"
            )
            invocation.stdout.write(envelope)
            return 0
        if job_id == "9.server":
            invocation.stdout.write(
                json.dumps({"Jobs": {"9.server": {"job_state": "R"}}})
            )
            return 0
        invocation.stderr.write(f"qstat: Unknown Job Id {job_id}\n")
        invocation.stdout.write(envelope)
        return 153

    _login_shell(ssh_server, qstat=qstat)

    async with await _connect(ssh_server) as host:
        scheduler = _pbs(host)
        running = await scheduler.inspect("9.server")
        ended = await scheduler.inspect("8.server")

    assert running.state is JobState.RUNNING
    assert ended.state is JobState.ENDED


async def test_interactive_submission_on_the_login_node(
    ssh_server: SSHTestServer, adapter: Adapter
):
    async def submit(invocation: Invocation) -> None:
        assert invocation.term_type is not None
        invocation.stdout.write(adapter.submitted)
        invocation.stderr.write(f"started {await invocation.stdin.readline()}")

    _login_shell(ssh_server, **{adapter.submit_command: submit})

    async with await _connect(ssh_server) as host:
        job = await adapter.create(host).submit_interactive(
            JobSpec(command=(_COMMAND, "two words")), startup_timeout=WAIT_SECONDS
        )
        await job.start_command()
        output: list[bytes] = []
        await asyncio.wait_for(job.drain(output.append), WAIT_SECONDS)
        job.close()

    assert job.job_id == adapter.job_id
    assert b"".join(output) == (
        f"{adapter.submitted}started exec {_COMMAND} 'two words'\n".encode()
    )


async def test_interactive_submission_that_exits_on_the_login_node(
    ssh_server: SSHTestServer, adapter: Adapter
):
    _login_shell(
        ssh_server,
        **{adapter.submit_command: reply(stderr="queue is closed\n", exit_status=2)},
    )

    async with await _connect(ssh_server) as host:
        with pytest.raises(
            SchedulerError,
            match=r"exited before returning a job ID \(2\): queue is closed$",
        ):
            await adapter.create(host).submit_interactive(
                JobSpec(command=("true",)), startup_timeout=WAIT_SECONDS
            )
