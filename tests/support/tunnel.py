"""
Running `ezhpcy tunnel` against the in-process SSH server (use the `login` fixture).

The worker job is played by the tests: a scheduler fake, or fake scheduler
commands on the server, write the worker's `ready` status into the run's control
directory, and `SSHTestServer.listen` stands in for the worker's sshd.
"""

import asyncio
import logging
import shlex
import socket
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ezhpcy.config import config
from ezhpcy.constants import EZHPCY_VERSION
from ezhpcy.job import lifecycle
from ezhpcy.scheduler.base import InteractiveJob, JobInfo, JobSpec, JobState
from ezhpcy.ssh import RemoteHost
from ezhpcy.types import ProfileConfig
from tests.support.login import STATE, remote_state_json
from tests.support.sshserver import Handler, Invocation, SSHTestServer, reply

BANNER = b"SSH-2.0-worker\r\n"
JOB_ID = "42"
CONTROL_ROOT = STATE / "ezhpcy" / EZHPCY_VERSION / "worker_control"
# A few milliseconds each, so every scenario takes well under a second.
FAST_TIMINGS = {
    "queue_timeout_seconds": 5,
    "worker_startup_timeout_seconds": 5,
    "job_poll_interval_seconds": 0.01,
    "job_monitor_interval_seconds": 0.01,
    "worker_heartbeat_interval_seconds": 0.01,
    "worker_heartbeat_timeout_seconds": 1,
}
# The interactive job's shell, as `FakeScheduler` starts it on the login node.
INTERACTIVE_JOB = "fake-interactive-job"


def configure_tunnel(
    monkeypatch: pytest.MonkeyPatch, server: SSHTestServer, **profile: object
) -> None:
    """
    Configure profile `base` (LSF, interactive, `FAST_TIMINGS`; `profile` adds to
    or overrides its settings), with automatic provisioning against `server`.
    """
    settings: dict[str, object] = {
        "connection": {"host": "login.example.com", "user": "alice"},
        "scheduler": {"type": "LSF", "submission_mode": "interactive"},
        "timings": FAST_TIMINGS,
    }
    monkeypatch.setattr(
        config, "profile", {"base": ProfileConfig.model_validate(settings | profile)}
    )
    monkeypatch.setattr(config, "auto_provision", True)
    # Provisioning: the Pixi install and the two `pixi exec` runs of sshd.
    server.command("bash -c", reply())
    server.command("env", reply())


def serve_login_shell(server: SSHTestServer, **fakes: Handler) -> None:
    """
    Serve `bash -lc '<command>'` with the fake named after the command's first
    word, which sees the command's own words. `remote_state`'s `printf` is served
    already.
    """
    fakes = {"printf": reply(stdout=remote_state_json()), **fakes}

    async def dispatch(invocation: Invocation) -> int | None:
        argv = shlex.split(invocation.argv[2])
        result = fakes[argv[0]](Invocation(argv, invocation.process))
        return await result if isinstance(result, Awaitable) else result

    server.command("bash -lc", dispatch)


def write_worker_status(server: SSHTestServer, state: str, *fields: str) -> None:
    """Write a status as the worker does, in the only run's control directory."""
    [directory] = server.local_path(CONTROL_ROOT).iterdir()
    content = " ".join((directory.name, state, *fields))
    (directory / "status").write_text(f"{content}\n", encoding="utf-8")


def control_dirs(server: SSHTestServer) -> list[Path]:
    root = server.local_path(CONTROL_ROOT)
    return list(root.iterdir()) if root.exists() else []


def unused_port() -> int:
    """A port nothing listens on, which the login node may not forward to."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def banner_then_echo(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """The worker's sshd: a banner at once, then what the client sent, after its EOF."""
    writer.write(BANNER)
    writer.write(b"worker:" + await reader.read())
    writer.close()


def banner(text: bytes) -> Callable[..., Awaitable[None]]:
    """A worker that sends `text`, then waits for the client to hang up."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        writer.write(text)
        await reader.read()
        writer.close()

    return handler


def snapshot(state: JobState, raw_state: str, host: str | None = None) -> JobInfo:
    return JobInfo(state=state, raw_state=raw_state, host=host)


RUNNING = snapshot(JobState.RUNNING, "RUN", "127.0.0.1")

type Inspection = JobInfo | Exception | Callable[[], JobInfo]


class FakeScheduler:
    """
    Stands in for a scheduler adapter, with the interactive job's shell a real
    process on the test server (see `serve_interactive_job`).

    - `inspect` replays `inspections`, the last one repeating. An exception is
      raised, and a callable is called for the snapshot.
    - `submitted` is called with the job's spec once it is submitted, e.g. to write
      the worker's `ready` status.
    - `submit` (batch) then awaits `submitting` before it returns the job's ID.
    - `cancel` records the job and then calls `cancelling`.
    """

    cancel_executable = "bkill"

    def __init__(
        self,
        host: RemoteHost,
        inspections: list[Inspection],
        *,
        submitted: Callable[[JobSpec], None] = lambda _spec: None,
        submitting: Callable[[], Awaitable[None]] | None = None,
        cancelling: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._host = host
        self._inspections = list(inspections)
        self._submitted = submitted
        self._submitting = submitting
        self._cancelling = cancelling
        self.cancelled: list[str] = []

    async def submit_interactive(
        self, spec: JobSpec, *, startup_timeout: float
    ) -> InteractiveJob:
        process = await self._host.start_login_shell([INTERACTIVE_JOB])
        self._submitted(spec)
        return InteractiveJob(
            job_id=JOB_ID,
            process=process,
            submission_command=(INTERACTIVE_JOB,),
            shell_command=shlex.join(["exec", *spec.command]),
        )

    async def submit(self, spec: JobSpec) -> str:
        self._submitted(spec)
        if self._submitting is not None:
            await self._submitting()
        return JOB_ID

    async def inspect(self, _job_id: str) -> JobInfo:
        entry = (
            self._inspections.pop(0)
            if len(self._inspections) > 1
            else self._inspections[0]
        )
        if isinstance(entry, Exception):
            raise entry
        return entry() if callable(entry) else entry

    async def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)
        if self._cancelling is not None:
            await self._cancelling()


def serve_interactive_job(server: SSHTestServer) -> None:
    """Serve `FakeScheduler`'s interactive job: say it is submitted, then wait."""

    async def job(invocation: Invocation) -> None:
        invocation.stdout.write(f"Job <{JOB_ID}> is submitted to queue <normal>.\n")
        # Until the tunnel closes the job.
        while await invocation.stdin.readline():
            pass

    server.command(f"bash -lc {INTERACTIVE_JOB}", job)


def use_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[RemoteHost], FakeScheduler],
) -> list[FakeScheduler]:
    """Give the tunnel `make(host)` as its scheduler; returns those it made."""
    made: list[FakeScheduler] = []

    def create(_resolved: object, host: RemoteHost) -> FakeScheduler:
        made.append(make(host))
        return made[-1]

    monkeypatch.setattr(lifecycle, "create_scheduler", create)
    return made


class LogRecords(logging.Handler):
    """The tunnel's log records (the `ezhpcy` logger doesn't propagate)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int = logging.DEBUG) -> list[str]:
        return [
            record.getMessage() for record in self.records if record.levelno >= level
        ]

    def starting(self, prefix: str) -> list[str]:
        return [message for message in self.messages() if message.startswith(prefix)]

    def command_errors(self) -> list[str]:
        """The errors the command reports as it exits."""
        return [
            record.getMessage()
            for record in self.records
            if record.name == "ezhpcy.cli.tunnel" and record.levelno >= logging.ERROR
        ]


@contextmanager
def tunnel_log() -> Generator[LogRecords]:
    """Collect the records of the tunnel's modules, debug ones included."""
    handler = LogRecords()
    loggers = [
        logging.getLogger(name)
        for name in (
            "ezhpcy.app.session",
            "ezhpcy.cli.tunnel",
            "ezhpcy.job.lifecycle",
            "ezhpcy.tunnel.session",
            "ezhpcy.ssh",
        )
    ]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)
