"""
`ezhpcy tunnel` end to end: the session against the in-process SSH server, with
the worker played by the tests. How each failure shuts the tunnel down is in
`test_tunnel_shutdown.py`.
"""

import asyncio
import functools
import io
import itertools
import logging
import re
import signal
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ezhpcy.cli import tunnel as tunnel_cli
from ezhpcy.cli.utils import ssh as cli_ssh
from ezhpcy.config import LocalFileConfig
from ezhpcy.console import flush_output, make_console
from ezhpcy.constants import LOOPBACK_HOST
from ezhpcy.job import lifecycle
from ezhpcy.job.lifecycle import JobLifecycle
from ezhpcy.logging import REPLAY_ON_ERROR
from ezhpcy.scheduler.base import JobInfo, JobState
from ezhpcy.ssh import RemoteHost
from ezhpcy.tunnel import lock, session, ssh_config
from ezhpcy.tunnel.lock import alias_lock, alias_locked
from ezhpcy.tunnel.status import Phase, TunnelStatus
from ezhpcy.worker.control import WorkerControl
from tests.support.cli import invoke
from tests.support.sshserver import Invocation, SSHTestServer, reply
from tests.support.terminal import GatedTerminal, terminal_handler, use_terminal
from tests.support.tunnel import (
    BANNER,
    FAST_TIMINGS,
    INTERACTIVE_JOB,
    JOB_ID,
    RUNNING,
    FakeScheduler,
    banner,
    banner_then_echo,
    configure_tunnel,
    control_dirs,
    serve_interactive_job,
    serve_login_shell,
    snapshot,
    tunnel_log,
    use_scheduler,
    write_worker_status,
)

# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5
# How long a step takes to finish after Ctrl+C, so that the stop arrives meanwhile.
STOP_SECONDS = 0.1


def block_port(active_config: Path) -> int:
    """The port a tunnel's Host block points OpenSSH at."""
    [port] = [
        line.split()[1]
        for line in active_config.read_text(encoding="utf-8").splitlines()
        if line.split()[:1] == ["Port"]
    ]
    return int(port)


def connect_through(port: int, data: bytes) -> bytes:
    """As `ssh` does: connect to the tunnel's port, send `data` and EOF, and
    return everything that comes back."""
    received = b""
    with socket.create_connection((LOOPBACK_HOST, port), WAIT_SECONDS) as stream:
        stream.sendall(data)
        stream.shutdown(socket.SHUT_WR)
        while chunk := stream.recv(64 * 1024):
            received += chunk
    return received


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
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch, worker_port: int
) -> threading.Event:
    """
    Configure profile `base` with an interactive LSF job whose worker serves on
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
    configure_tunnel(monkeypatch, login)
    monkeypatch.setattr(ssh_config, "check_host_resolution", lambda _host, _port: None)
    return done


def test_a_tunnel_forwards_its_port_to_the_worker(
    login: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
    isolated_local_files: LocalFileConfig,
) -> None:
    worker_port = login.listen(banner_then_echo)
    running, done = threading.Event(), threading.Event()
    submissions: list[list[str]] = []
    worker_commands: list[str] = []

    async def bsub(invocation: Invocation) -> None:
        assert invocation.term_type is not None
        submissions.append(invocation.argv)
        invocation.stdout.write(f"Job <{JOB_ID}> is submitted to queue <normal>.\n")
        worker_commands.append(await invocation.stdin.readline())
        # The worker's sshd listens, and says so.
        write_worker_status(login, "ready", str(worker_port))
        running.set()
        invocation.stdout.write("<<Starting on 127.0.0.1>>\n")
        while await invocation.stdin.readline():
            pass

    serve_login_shell(login, bsub=bsub, bjobs=bjobs(running, done), bkill=reply())
    configure_tunnel(
        monkeypatch, login, resources={"queue": "normal", "cores": 4, "memory": "2GiB"}
    )
    monkeypatch.setattr(ssh_config, "check_host_resolution", lambda _host, _port: None)
    active_config = isolated_runtime_dir / "ssh-config" / "active-scratch.conf"
    # Left by a tunnel that was killed.
    stale_config = ssh_config.write_active_host_config(
        ssh_config.WorkerHost.for_endpoint(
            "gone", user="alice", host="login.example.com"
        ),
        2222,
    )
    published: list[bool] = []
    outputs: list[bytes] = []

    def publish_then_connect(host: ssh_config.WorkerHost, port: int) -> None:
        ssh_config.publish_host(host, port)
        published.extend([active_config.exists(), stale_config.exists()])
        assert block_port(active_config) == port
        # As `ssh scratch` does, twice.
        outputs.extend(connect_through(port, data) for data in (b"hello", b"again"))
        # Then the job ends.
        done.set()

    monkeypatch.setattr(session, "publish_host", publish_then_connect)
    previous_handler = signal.getsignal(signal.SIGINT)

    with tunnel_log() as log:
        result = invoke(
            ["tunnel", "base", "--alias", "scratch", "--worker-port", str(worker_port)]
        )

    assert result.exit_code == 0, result
    assert outputs == [BANNER + b"worker:hello", BANNER + b"worker:again"]
    # The interactive job's output is logged, its ID included.
    job_output = "\n".join(log.messages())
    assert "Job <42> is submitted to queue <normal>." in job_output
    assert "<<Starting on 127.0.0.1>>" in job_output
    # To a log file named after the alias, not the profile.
    assert flush_output(5)
    [log_file] = isolated_local_files.log_dir.iterdir()
    assert re.fullmatch(r"ezhpcy-\d{8}-\d{6}-scratch-\d+\.log", log_file.name)

    [submission] = submissions
    assert submission[:2] == ["bsub", "-Is"]
    assert submission[submission.index("-q") + 1] == "normal"
    assert submission[submission.index("-n") + 1] == "4"
    assert "rusage[mem=2048MB]" in submission
    [worker_command] = worker_commands
    assert worker_command.startswith("cd /home/alice/.cache/ezhpcy/")
    assert " && exec env PIXI_HOME=" in worker_command

    assert (
        log.messages(logging.INFO).count(
            "Stopping tunnel: worker job 42 ended in scheduler state DONE with exit code 0"
        )
        == 1
    )
    # The job ended by itself, so it isn't cancelled.
    assert not any(
        argv[:2] == ["bash", "-lc"] and argv[2].startswith("bkill")
        for argv in login.commands_run
    )

    assert len(log.starting("Forwarded connection opened: client=127.0.0.1:")) == 2
    # Nothing else says so: the log and the status display are on stderr.
    assert result.stdout == ""

    # The Host block was there while serving, the stale one was pruned, and
    # everything is cleaned up.
    assert published == [True, False]
    assert not active_config.exists()
    assert not alias_locked("scratch")
    assert control_dirs(login) == []
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_a_terminal_that_takes_no_output_does_not_stop_the_tunnel(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With debug logging, each connection logs. Written from the event loop, a
    terminal that took no output froze every session (FINDINGS.md, "Phase 4").
    """
    worker_port = login.listen(banner_then_echo)
    done = serve_lsf_job(login, monkeypatch, worker_port)
    terminal = GatedTerminal()
    terminal.opened.set()
    monkeypatch.setattr(terminal_handler(), "console", make_console(file=terminal))
    outputs: list[bytes] = []
    blocked_while_serving: list[bool] = []

    def publish_then_connect(host: ssh_config.WorkerHost, port: int) -> None:
        ssh_config.publish_host(host, port)
        # The terminal stops taking output once the tunnel serves.
        terminal.opened.clear()
        try:
            for number in range(10):
                outputs.append(connect_through(port, b"%d" % number))
            blocked_while_serving.append(not flush_output(0))
        finally:
            # Also if a connection hung, so the test fails rather than hangs.
            terminal.opened.set()
            done.set()

    monkeypatch.setattr(session, "publish_host", publish_then_connect)

    result = invoke(
        [
            "--debug",
            "tunnel",
            "base",
            "--alias",
            "scratch",
            "--worker-port",
            str(worker_port),
        ]
    )

    assert result.exit_code == 0, result
    assert outputs == [BANNER + b"worker:%d" % number for number in range(10)]
    assert blocked_while_serving == [True]
    # Nothing is lost once the terminal takes output again.
    assert terminal.getvalue().count("Forwarded connection opened") == 10


class Recorded:
    """An instance attribute that keeps every value it is set to, in `values`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.values: list[object] = []

    def __get__(self, instance: object, owner: type | None = None) -> object:
        return self if instance is None else vars(instance)[self.name]

    def __set__(self, instance: object, value: object) -> None:
        vars(instance)[self.name] = value
        self.values.append(value)


def test_the_status_follows_the_tunnel_from_connecting_until_it_stops(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner_then_echo)
    done = serve_lsf_job(login, monkeypatch, worker_port)
    recorded = Recorded("status")
    monkeypatch.setattr(JobLifecycle, "status", recorded, raising=False)

    def publish_then_connect(host: ssh_config.WorkerHost, port: int) -> None:
        ssh_config.publish_host(host, port)
        connect_through(port, b"hello")
        done.set()

    monkeypatch.setattr(session, "publish_host", publish_then_connect)

    result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    statuses = [
        status for status in recorded.values if isinstance(status, TunnelStatus)
    ]
    assert len(statuses) == len(recorded.values)
    assert [phase for phase, _ in itertools.groupby(s.phase for s in statuses)] == [
        Phase.CONNECTING,
        Phase.PREPARING,
        Phase.SUBMITTING,
        Phase.QUEUED,
        Phase.STARTING,
        Phase.SERVING,
        Phase.STOPPING,
    ]
    assert statuses[0].alias == "base"
    assert statuses[0].login == "alice@login.example.com"
    serving = next(s for s in statuses if s.phase is Phase.SERVING)
    assert serving.job_id == JOB_ID
    assert serving.job is not None
    assert serving.job.state is JobState.RUNNING
    assert serving.running_since is not None
    assert serving.worker == f"127.0.0.1:{worker_port}"
    # One connection was open, and closed by the end.
    assert max(s.connections for s in statuses) == 1
    assert statuses[-1].connections == 0
    assert any(s.last_heartbeat is not None for s in statuses)


def test_on_a_terminal_the_status_display_shows_the_tunnel(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With debug logging, so that records are written under it all along."""
    worker_port = login.listen(banner_then_echo)
    done = serve_lsf_job(login, monkeypatch, worker_port)
    terminal = io.StringIO()
    use_terminal(monkeypatch, terminal)

    def publish_then_serve(host: ssh_config.WorkerHost, port: int) -> None:
        ssh_config.publish_host(host, port)
        # Long enough to be redrawn serving.
        threading.Timer(0.6, done.set).start()

    monkeypatch.setattr(session, "publish_host", publish_then_serve)

    result = invoke(["--debug", "tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    assert flush_output(WAIT_SECONDS)
    output = terminal.getvalue()
    assert "Connecting to alice@login.example.com" in output
    assert "Connections" in output
    assert "Stopping tunnel: worker job 42 ended" in output


def test_open_sessions_are_cut_when_the_tunnel_stops(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As `ssh` sees it: a session still open when the tunnel stops ends with it."""
    # A worker that keeps each session open until the client hangs up.
    worker_port = login.listen(banner(BANNER))
    done = serve_lsf_job(login, monkeypatch, worker_port)
    watchers: list[threading.Thread] = []
    endings: list[bytes | str] = []

    def wait_for_the_end(stream: socket.socket) -> None:
        with stream:
            try:
                endings.append(stream.recv(64 * 1024))
            except OSError as error:
                endings.append(type(error).__name__)

    def publish_then_open_a_session(host: ssh_config.WorkerHost, port: int) -> None:
        ssh_config.publish_host(host, port)
        stream = socket.create_connection((LOOPBACK_HOST, port), WAIT_SECONDS)
        received = b""
        while len(received) < len(BANNER):
            received += stream.recv(len(BANNER) - len(received))
        assert received == BANNER
        watchers.append(threading.Thread(target=wait_for_the_end, args=(stream,)))
        watchers[0].start()
        # Then the job ends, and the tunnel stops.
        done.set()

    monkeypatch.setattr(session, "publish_host", publish_then_open_a_session)

    result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    [watcher] = watchers
    watcher.join(WAIT_SECONDS)
    # End of stream or a reset, not a timeout: the session didn't outlive the tunnel.
    assert endings in ([b""], ["ConnectionResetError"], ["ConnectionAbortedError"])


def test_a_second_tunnel_for_a_served_alias_is_refused_before_connecting(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_tunnel(monkeypatch, login)

    with alias_lock("base"), tunnel_log() as log:
        result = invoke(["tunnel", "base"])

    assert result.exit_code == 1, result
    assert log.command_errors() == [
        "Could not start tunnel: a tunnel is already running for base"
    ]
    # Before any password prompt.
    assert login.auth_attempts == []


def test_shutdown_withdraws_the_block_before_closing_the_port(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And releases the alias lock last."""
    worker_port = login.listen(banner(BANNER))
    events: list[str] = []

    @contextmanager
    def recording_lock(alias: str) -> Generator[None]:
        with lock.alias_lock(alias):
            events.append("take the lock")
            yield
        events.append("release the lock")

    connect = cli_ssh.connect_interactive

    async def recording_connect(*args: object, **kwargs: object) -> RemoteHost:
        events.append("connect")
        return await connect(*args, **kwargs)  # type: ignore[arg-type]

    forward = RemoteHost.forward_local_port

    async def recording_forward(host: RemoteHost, *args: object, **kwargs: object):
        listener = await forward(host, *args, **kwargs)  # type: ignore[arg-type]
        close = listener.close

        def recording_close() -> None:
            events.append("close the forwarded port")
            close()

        listener.close = recording_close
        return listener

    close_login = RemoteHost.close

    async def recording_close_login(host: RemoteHost) -> None:
        events.append("close the login connection")
        await close_login(host)

    def press() -> object:
        signal.raise_signal(signal.SIGINT)
        return RUNNING

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, press, RUNNING],
            submitted=lambda _spec: write_worker_status(
                login, "ready", str(worker_port)
            ),
        ),
    )
    monkeypatch.setattr(session, "alias_lock", recording_lock)
    monkeypatch.setattr(cli_ssh, "connect_interactive", recording_connect)
    monkeypatch.setattr(RemoteHost, "forward_local_port", recording_forward)
    monkeypatch.setattr(RemoteHost, "close", recording_close_login)
    monkeypatch.setattr(
        session, "publish_host", lambda _host, _port: events.append("publish")
    )
    monkeypatch.setattr(
        session, "withdraw_host", lambda _host: events.append("withdraw")
    )

    result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    assert events == [
        "take the lock",
        "connect",
        "publish",
        "withdraw",
        "close the forwarded port",
        "close the login connection",
        "release the lock",
    ]
    assert not alias_locked("base")


class _OutputJob:
    """An interactive job that has written `chunks`."""

    job_id = JOB_ID

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.drained: list[bytes] = []

    async def drain(self, sink: Callable[[bytes], Awaitable[object]]) -> None:
        for chunk in self.chunks:
            self.drained.append(chunk)
            await sink(chunk)


async def test_interactive_output_is_logged_line_by_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=lifecycle.__name__)
    # "Velkommen på" with its "å" split and a terminal's line ending, then a
    # cut-off "ø" without one.
    job = _OutputJob(b"Velkommen p\xc3", b"\xa5\r\nHej", b"\n\n\xc3")

    await lifecycle._log_job_output(job)  # type: ignore[arg-type]

    # All read, so that the job doesn't stall.
    assert job.drained == list(job.chunks)
    assert caplog.messages == ["Velkommen på", "Hej", "", "\ufffd"]
    assert {record.levelno for record in caplog.records} == {logging.DEBUG}
    # To show before an error, by a terminal that didn't.
    assert all(
        record.__dict__.items() >= REPLAY_ON_ERROR.items() for record in caplog.records
    )


@pytest.mark.parametrize(
    ("output", "shown"),
    [
        # Bash echoing a line of the worker script, around bracketed paste.
        (b"\x1b[?2004l\r\x1b[?2004h>     fi\r\n", ">     fi"),
        (b"\x1b[1;31mError\x1b[0m: no sshd\n", "Error: no sshd"),
        (b"\x1b(B\x1b[m$ \n", "$ "),
        (b"\x1b=\x1b>ready\x07\n", "ready"),
        # A progress line, written over.
        (b"10%\r50%\r100%\n", "100%"),
        pytest.param(
            b"\x1b]0;user@n-62-12-19: ~\x07$ \n",
            "$ ",
            marks=pytest.mark.xfail(
                reason="Rich only knows a window title that ends with ESC \\, not "
                "BEL; bash sets one only for xterm-like terminals, not our vt100",
                strict=True,
            ),
            id="window-title-ending-with-bel",
        ),
    ],
)
async def test_interactive_output_is_logged_as_a_terminal_shows_it(
    caplog: pytest.LogCaptureFixture, output: bytes, shown: str
) -> None:
    caplog.set_level(logging.DEBUG, logger=lifecycle.__name__)

    await lifecycle._log_job_output(_OutputJob(output))  # type: ignore[arg-type]

    assert caplog.messages == [shown]


def test_a_failed_interactive_tunnel_shows_the_end_of_the_job_output(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = threading.Event()

    async def job(invocation: Invocation) -> None:
        invocation.stdout.write(f"Job <{JOB_ID}> is submitted to queue <normal>.\n")
        # As the job's terminal echoes the worker command, then its error.
        invocation.stdout.write(await invocation.stdin.readline())
        # The scheduler says the job failed before its error arrives; then the
        # submission ends.
        failing.set()
        await asyncio.sleep(STOP_SECONDS)
        invocation.stdout.write("sshd must resolve to an absolute path\n")

    login.command(f"bash -lc {INTERACTIVE_JOB}", job)
    configure_tunnel(monkeypatch, login)
    pending = snapshot(JobState.PENDING, "PEND")
    failed = snapshot(JobState.FAILED, "EXIT")
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host, [lambda: failed if failing.is_set() else pending]
        ),
    )

    # Without debug logging, which would show the output as it arrives.
    result = invoke(["tunnel", "base"])

    assert result.exit_code == 1, result
    # All of it, as it led up to the error: it is well within the limit.
    stderr = " ".join(result.stderr.split())
    assert f"] Job <{JOB_ID}> is submitted to queue <normal>." in stderr
    failure = stderr.index("ERROR [ezhpcy.cli.tunnel] Could not start tunnel")
    assert 0 <= stderr.find("] sshd must resolve to an absolute path") < failure
    # The job's output, not the session's own debug records.
    assert "Cleaning up the tunnel" not in stderr


def test_an_lsf_batch_tunnel_sends_the_worker_script_to_bsub(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner(BANNER))
    running, done = threading.Event(), threading.Event()
    scripts: list[str] = []

    async def bsub(invocation: Invocation) -> None:
        assert invocation.term_type is None
        scripts.append(await invocation.stdin.read())
        write_worker_status(login, "ready", str(worker_port))
        running.set()
        invocation.stdout.write(f"Job <{JOB_ID}> is submitted to queue <normal>.\n")

    serve_login_shell(login, bsub=bsub, bjobs=bjobs(running, done), bkill=reply())
    configure_tunnel(
        monkeypatch, login, scheduler={"type": "LSF", "submission_mode": "batch"}
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: done.set())

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    [script] = scripts
    assert script.startswith("#!/usr/bin/env bash\nexec env PIXI_HOME=")
    assert " bash -c " in script
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 " in script
    assert log.starting("Tunnel ready for base via worker 127.0.0.1:")


def test_keepalive_loss_while_serving_stops_the_tunnel(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner(BANNER))
    monkeypatch.setattr(
        cli_ssh,
        "connect_interactive",
        functools.partial(
            cli_ssh.connect_interactive, keepalive_interval=0.1, keepalive_count_max=2
        ),
    )

    def unplug() -> object:
        login.blackhole()
        return RUNNING

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    schedulers = use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, unplug, RUNNING],
            submitted=lambda _spec: write_worker_status(
                login, "ready", str(worker_port)
            ),
        ),
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: None)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 1, result
    assert log.command_errors() == [
        (
            "Tunnel stopped: login-node SSH connection lost: Server not responding "
            "to keepalive"
        )
    ]
    [scheduler] = schedulers
    # The fake doesn't need the connection; a real `bkill` would fail.
    assert scheduler.cancelled == [JOB_ID]


def test_a_job_waiting_in_the_queue_says_why_once(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_tunnel(
        monkeypatch,
        login,
        timings=FAST_TIMINGS | {"queue_timeout_seconds": 0.35},
    )
    serve_interactive_job(login)
    waiting = JobInfo(
        state=JobState.PENDING,
        raw_state="PEND",
        pending_reason="Job slot limit reached: 3 hosts",
    )
    use_scheduler(monkeypatch, lambda host: FakeScheduler(host, [waiting]))

    with tunnel_log() as log:
        result = invoke(["tunnel", "base"])

    assert result.exit_code == 1, result
    # Polled several times, but said only as the state changes: the status
    # display shows how long it has waited.
    assert log.starting(f"Worker job {JOB_ID}") == [
        f"Worker job {JOB_ID}: pending (PEND): Job slot limit reached: 3 hosts."
    ]


def test_the_worker_startup_timeout_covers_both_waits(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeout_seconds = 1.0
    # So that the worker's status is seen as soon as it is written.
    monkeypatch.setattr(lifecycle, "_STATUS_POLL_SECONDS", 0.01)
    worker_port = login.listen(banner(b"not an ssh server\n"))
    configure_tunnel(
        monkeypatch,
        login,
        timings=FAST_TIMINGS | {"worker_startup_timeout_seconds": timeout_seconds},
    )
    serve_interactive_job(login)

    def start_worker_late(_spec: object) -> None:
        # Most of the budget goes on waiting for `ready`, which leaves too
        # little for the banner, which never comes anyway.
        threading.Timer(
            0.7 * timeout_seconds,
            write_worker_status,
            (login, "ready", str(worker_port)),
        ).start()

    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(host, [RUNNING], submitted=start_worker_late),
    )

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 1, result
    [error] = log.command_errors()
    assert "did not become ready within 1 seconds" in error
    [allocated] = [
        record for record in log.records if record.getMessage().startswith("Worker job")
    ]
    [stopping] = [
        record
        for record in log.records
        if record.getMessage().startswith("Stopping tunnel: ")
    ]
    # Two budgets would take 1.7 seconds.
    assert stopping.created - allocated.created < 1.4 * timeout_seconds


@pytest.mark.parametrize(
    "interrupt",
    [
        pytest.param(signal.SIGINT, id="SIGINT"),
        # Ctrl+Break, on Windows only.
        pytest.param(
            getattr(signal, "SIGBREAK", None),
            marks=pytest.mark.skipif(
                not hasattr(signal, "SIGBREAK"), reason="Windows only"
            ),
            id="SIGBREAK",
        ),
    ],
)
def test_a_second_interrupt_quits_without_cleaning_up(
    login: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: signal.Signals,
) -> None:
    worker_port = login.listen(banner(BANNER))
    exits: list[int] = []
    # In place of ending the test run; the cleanup then simply carries on.
    monkeypatch.setattr(tunnel_cli.os, "_exit", exits.append)

    def press() -> object:
        signal.raise_signal(interrupt)
        return RUNNING

    async def press_again() -> None:
        signal.raise_signal(interrupt)

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, press, RUNNING],
            submitted=lambda _spec: write_worker_status(
                login, "ready", str(worker_port)
            ),
            cancelling=press_again,
        ),
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: None)
    previous_handler = signal.getsignal(interrupt)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    assert exits == [130]
    assert "Stopping tunnel: Ctrl+C" in log.messages()
    assert log.starting("Quitting without cleaning up.")
    assert signal.getsignal(interrupt) is previous_handler


def test_a_cleanup_step_that_hangs_is_given_up_on(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner(BANNER))
    monkeypatch.setattr(lifecycle, "_CLEANUP_SECONDS", 0.2)

    async def hang() -> None:
        await asyncio.Event().wait()

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, snapshot(JobState.UNKNOWN, "UNKWN")],
            submitted=lambda _spec: write_worker_status(
                login, "ready", str(worker_port)
            ),
            cancelling=hang,
        ),
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: None)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 1, result
    assert log.starting("Could not cancel worker job 42: no answer within 0.2 seconds.")
    # Cleanup carried on after the job.
    assert control_dirs(login) == []


def test_a_lock_release_that_hangs_is_given_up_on(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner(BANNER))
    monkeypatch.setattr(session, "_LOCK_RELEASE_SECONDS", 0.1)

    @contextmanager
    def slow_lock(alias: str) -> Generator[None]:
        with lock.alias_lock(alias):
            try:
                yield
            finally:
                time.sleep(0.5)

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, snapshot(JobState.SUCCEEDED, "DONE")],
            submitted=lambda _spec: write_worker_status(
                login, "ready", str(worker_port)
            ),
        ),
    )
    monkeypatch.setattr(session, "alias_lock", slow_lock)
    monkeypatch.setattr(session, "publish_host", lambda _host, _port: None)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    assert log.starting(
        "Could not release the alias lock: no answer within 0.1 seconds."
    )


@pytest.mark.parametrize("hangs", [False, True], ids=["finishes", "hangs"])
def test_ctrl_c_during_batch_submission_lets_it_finish(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch, hangs: bool
) -> None:
    """Cancelled midway, `qsub` could queue the job without returning its ID."""
    monkeypatch.setattr(lifecycle, "_CLEANUP_SECONDS", 0.2)

    async def press() -> None:
        signal.raise_signal(signal.SIGINT)
        await (asyncio.Event().wait() if hangs else asyncio.sleep(STOP_SECONDS))

    configure_tunnel(
        monkeypatch, login, scheduler={"type": "PBS", "submission_mode": "batch"}
    )
    schedulers = use_scheduler(
        monkeypatch, lambda host: FakeScheduler(host, [RUNNING], submitting=press)
    )

    with tunnel_log() as log:
        result = invoke(["tunnel", "base"])

    assert result.exit_code == 0, result
    assert "Stopping tunnel: Ctrl+C" in log.messages()
    [scheduler] = schedulers
    if hangs:
        # Given up on, with a warning.
        assert scheduler.cancelled == []
        assert log.starting(
            "Gave up waiting to submit the worker job after 0.2 seconds"
        )
    else:
        assert scheduler.cancelled == [JOB_ID]
    assert control_dirs(login) == []


def _stop_from_thread() -> None:
    """Press Ctrl+C from a step run in a thread, and give the stop time to arrive."""
    signal.raise_signal(signal.SIGINT)
    time.sleep(STOP_SECONDS)


def _stop_after_creating_the_control_directory(
    monkeypatch: pytest.MonkeyPatch, _published: list[str]
) -> None:
    create = WorkerControl.create

    async def create_then_stop(control: WorkerControl) -> None:
        await create(control)
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(STOP_SECONDS)

    monkeypatch.setattr(WorkerControl, "create", create_then_stop)


def _stop_before_publishing_the_host_block(
    monkeypatch: pytest.MonkeyPatch, published: list[str]
) -> None:
    def stop_then_publish(host: ssh_config.WorkerHost, _port: int) -> None:
        _stop_from_thread()
        published.append(host.alias)

    monkeypatch.setattr(session, "publish_host", stop_then_publish)


@pytest.mark.parametrize(
    "stop_during_step",
    [
        _stop_after_creating_the_control_directory,
        _stop_before_publishing_the_host_block,
    ],
    ids=["control-directory", "host-block"],
)
def test_ctrl_c_during_a_step_still_cleans_up_after_it(
    login: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    stop_during_step: Callable[[pytest.MonkeyPatch, list[str]], None],
) -> None:
    """Cancelled midway, the step would still take effect, with no cleanup for it."""
    worker_port = login.listen(banner(BANNER))
    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING],
            submitted=lambda _spec: write_worker_status(
                login, "ready", str(worker_port)
            ),
        ),
    )
    published: list[str] = []
    withdrawn: list[str] = []
    monkeypatch.setattr(
        session, "publish_host", lambda host, _port: published.append(host.alias)
    )
    monkeypatch.setattr(
        session, "withdraw_host", lambda host: withdrawn.append(host.alias)
    )
    stop_during_step(monkeypatch, published)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    assert "Stopping tunnel: Ctrl+C" in log.messages()
    assert control_dirs(login) == []
    assert published == withdrawn
    assert not alias_locked("base")


def test_ctrl_c_at_a_prompt_exits_130(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    configure_tunnel(monkeypatch, login)
    monkeypatch.setattr(cli_ssh, "connect_interactive", interrupted)

    result = invoke(["tunnel", "base"])

    assert result.exit_code == 130, result


def test_a_failed_login_is_reported(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    login.auth.client_keys = []
    configure_tunnel(monkeypatch, login)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base"])

    assert result.exit_code == 1, result
    [error] = log.command_errors()
    assert error.startswith("Could not start tunnel: Permission denied")
