"""
Running `ezhpcy tunnel` against the in-process SSH server (use the `login` fixture).

The worker job is played by the tests: a scheduler fake, or fake scheduler
commands on the server, write the worker's `ready` status into the run's control
directory, and a second SSH server stands in for the worker's sshd (the `worker`
fixture).
"""

import asyncio
import logging
import shlex
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ezhpcy.config import config
from ezhpcy.constants import EZHPCY_VERSION
from ezhpcy.job import lifecycle
from ezhpcy.scheduler.base import InteractiveJob, JobInfo, JobSpec, JobState
from ezhpcy.ssh import RemoteHost
from ezhpcy.tunnel import ssh_config
from ezhpcy.types import ProfileConfig
from tests.support.login import STATE, remote_state_json
from tests.support.sshserver import Handler, Invocation, SSHTestServer, reply

JOB_ID = "42"
CONTROL_ROOT = STATE / "ezhpcy" / EZHPCY_VERSION / "worker_control"
# A few milliseconds each, so every scenario takes well under a second.
FAST_TIMINGS = {
    "queue_timeout": 5,
    "worker_startup_timeout": 5,
    "job_poll_interval": 0.01,
    "job_monitor_interval": 0.01,
    "worker_heartbeat_interval": 0.01,
    "worker_heartbeat_timeout": 1,
}
# The interactive job's shell, as `FakeScheduler` starts it on the login node.
INTERACTIVE_JOB = "fake-interactive-job"
# How often `write_atomically` tries to rename, 10 ms apart.
_REPLACE_ATTEMPTS = 100


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


def bjobs(running: threading.Event, done: threading.Event):
    """LSF's `bjobs`: pending until `running`, then running until `done`."""

    def handler(invocation: Invocation) -> None:
        if done.is_set():
            state, exit_code = "DONE", "0"
        elif running.is_set():
            state, exit_code = "RUN", "-"
        else:
            state, exit_code = "PEND", "-"
        host = "-" if state == "PEND" else "127.0.0.1"
        reason = "Job slot limit reached: 3 hosts;" if state == "PEND" else "-"
        invocation.stdout.write(f"{JOB_ID}|{state}|{host}|{exit_code}|{reason}\n")

    return handler


def serve_lsf_job(
    login: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    worker_port: int,
    **profile: object,
) -> threading.Event:
    """
    Configure profile `base` (`profile` adds to its settings, as for
    `configure_tunnel`) with an interactive LSF job whose worker serves on
    `worker_port` at once. The job runs until the returned event is set.
    """
    running, done = threading.Event(), threading.Event()

    async def bsub(invocation: Invocation) -> None:
        invocation.stdout.write(f"Job <{JOB_ID}> is submitted to queue <normal>.\n")
        await invocation.stdin.readline()
        write_worker_status(login, "ready", str(worker_port))
        running.set()
        while await invocation.stdin.readline():
            pass

    serve_login_shell(login, bsub=bsub, bjobs=bjobs(running, done), bkill=reply())
    configure_tunnel(monkeypatch, login, **profile)
    monkeypatch.setattr(ssh_config, "check_host_resolution", lambda _host, _port: None)
    return done


def write_worker_status(server: SSHTestServer, state: str, *fields: str) -> None:
    """
    Write a status as the worker does, in the only run's control directory: by
    temporary file and rename, so that the job never reads half of it.
    """
    [directory] = server.local_path(CONTROL_ROOT).iterdir()
    content = " ".join((directory.name, state, *fields))
    write_atomically(directory / "status", f"{content}\n")


def write_atomically(path: Path, content: str) -> None:
    """
    Write `path` by temporary file and rename, as the worker writes its files.

    On Windows a file can't be replaced while the test server reads it, and the
    reader may wait for the client, whose event loop may be the caller's. So
    then the rename is tried again from a thread of its own, until the reader
    lets go.
    """
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    try:
        temporary.replace(path)
    except PermissionError:
        threading.Thread(target=_replace_soon, args=(temporary, path)).start()


def _replace_soon(temporary: Path, path: Path) -> None:
    for _ in range(_REPLACE_ATTEMPTS - 1):
        try:
            temporary.replace(path)
        except PermissionError:
            time.sleep(0.01)
        else:
            return
    temporary.replace(path)


def control_dirs(server: SSHTestServer) -> list[Path]:
    root = server.local_path(CONTROL_ROOT)
    return list(root.iterdir()) if root.exists() else []


def unused_port() -> int:
    """A port nothing listens on, which the login node may not forward to."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def banner(text: bytes) -> Callable[..., Awaitable[None]]:
    """
    For `SSHTestServer.listen`: a worker's port that sends `text`, then waits
    for the client to hang up, such as one that isn't an SSH server.
    """

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
    - After the first inspection, while `held()` is true, `inspect` reports
      the first one again instead of moving on: e.g. until the job is published,
      as the job monitor's 60 s interval would make sure outside tests.
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
        held: Callable[[], bool] = lambda: False,
    ) -> None:
        self._host = host
        self._inspections = list(inspections)
        self._first: Inspection | None = None
        self._held = held
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
        if self._first is not None and self._held():
            entry = self._first
        else:
            entry = (
                self._inspections.pop(0)
                if len(self._inspections) > 1
                else self._inspections[0]
            )
            if self._first is None:
                self._first = entry
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

    def create(_config: object, host: RemoteHost) -> FakeScheduler:
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
            "ezhpcy.app.run",
            "ezhpcy.cleanup",
            "ezhpcy.cli._resolve",
            "ezhpcy.cli.tunnel",
            "ezhpcy.config",
            "ezhpcy.forward",
            "ezhpcy.job.lifecycle",
            "ezhpcy.job.publish",
            "ezhpcy.login",
            "ezhpcy.process",
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


def openssh_resolves(
    monkeypatch: pytest.MonkeyPatch, resolve: Callable[[str], dict[str, list[str]]]
) -> None:
    """Answer `ssh -G <alias>` with `resolve(alias)`, OpenSSH's options by name."""
    monkeypatch.setattr(ssh_config, "resolve_alias", resolve)


def worker_ready_on(server: SSHTestServer, port: int) -> Callable[[JobSpec], None]:
    """
    For `FakeScheduler`'s `submitted`: each submitted job's worker says it is
    ready, its sshd on `port`, in its own control directory (named by the token
    in its log files' names).
    """

    def submitted(spec: JobSpec) -> None:
        assert spec.stdout_path is not None
        token = spec.stdout_path.name.removeprefix("worker-").removesuffix(".out")
        write_atomically(
            server.local_path(CONTROL_ROOT / token / "status"),
            f"{token} ready {port}\n",
        )

    return submitted
