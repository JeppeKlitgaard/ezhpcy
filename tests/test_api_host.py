"""
`ezhpcy.api`'s hosts: commands, files, processes started detached and forwarded
ports, on a plain host (`connect`) and on a job's node (`Job.host`), and how what
is attached to a host is closed with its connection. Uses `ezhpcy.api` alone,
with the in-process SSH servers and the fakes of `tests.support`.
"""

import asyncio
import json
import socket
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from ezhpcy import api
from tests.support.sshserver import Invocation, SSHTestServer, reply
from tests.support.tunnel import (
    FAST_TIMINGS,
    JOB_ID,
    RUNNING,
    FakeScheduler,
    configure_tunnel,
    control_dirs,
    unused_port,
    use_scheduler,
    worker_ready_on,
)
from tests.support.worker import NODE, FakeProcess, FakeProcesses, quick_processes

LOGIN = api.Login(host="login.example.com", user="alice")
JOB = api.JobConfig(
    scheduler=api.SchedulerSettings(
        type=api.SchedulerType.LSF, submission_mode=api.SubmissionMode.BATCH
    ),
    timings=api.Timings.model_validate(FAST_TIMINGS),
)
WAIT_SECONDS = 5


@pytest.fixture
def processes(login: SSHTestServer, monkeypatch: pytest.MonkeyPatch) -> FakeProcesses:
    """Processes started on the login node."""
    quick_processes(monkeypatch)
    return FakeProcesses(login)


async def _echo(reader: Any, writer: Any) -> None:
    """Echo until the client's EOF, then close."""
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()
    writer.close()


def round_trip(port: int, payload: bytes = b"ping") -> bytes:
    """Send `payload` and EOF to `port` on this machine; return what comes back."""
    received = b""
    with socket.create_connection(("127.0.0.1", port), WAIT_SECONDS) as stream:
        # A connection closed at once may be reset, or aborted (on Windows).
        try:
            stream.sendall(payload)
            stream.shutdown(socket.SHUT_WR)
            while chunk := stream.recv(64 * 1024):
                received += chunk
        except ConnectionResetError, ConnectionAbortedError:
            pass
    return received


def run_dir_exists(server: SSHTestServer, process: api.Process) -> bool:
    return server.local_path(process.run_dir).exists()


def signals(processes: FakeProcesses) -> list[tuple[int, str]]:
    """Each process group signalled, and with what, in order."""
    return [
        (int(argv[6]), argv[5])
        for argv in processes.server.commands_run
        if argv[:2] == ["bash", "-c"] and argv[3:4] == ["stop"]
    ]


# Commands and files


async def test_run_returns_stdout_and_raises_command_failed(
    login: SSHTestServer,
) -> None:
    async def cat(invocation: Invocation) -> None:
        invocation.stdout.write(await invocation.stdin.read())

    login.command("hostname", reply(stdout="login-1\n"))
    login.command("cat", cat)
    login.command("false", reply(stderr="nope\n", exit_status=1))

    async with api.connect(LOGIN) as host:
        assert await host.run(["hostname"]) == "login-1\n"
        assert await host.run(["cat"], stdin="hi\n") == "hi\n"
        with pytest.raises(api.CommandFailed) as failed:
            await host.run(["false"])

    assert str(failed.value) == "Remote command failed (1): nope"
    assert (failed.value.exit_status, failed.value.stderr) == (1, "nope\n")


async def test_put_and_get_copy_files(login: SSHTestServer, tmp_path: Path) -> None:
    local = tmp_path / "params.yaml"
    local.write_text("cores: 4\n", encoding="utf-8")
    remote = PurePosixPath("/home/alice/params.yaml")

    async with api.connect(LOGIN) as host:
        await host.put(local, remote)
        assert login.local_path(remote).read_text(encoding="utf-8") == "cores: 4\n"
        await host.get(remote, tmp_path / "back.yaml")

    assert (tmp_path / "back.yaml").read_text(encoding="utf-8") == "cores: 4\n"


# Processes


async def test_a_process_runs_detached_in_a_private_run_directory(
    login: SSHTestServer, processes: FakeProcesses
) -> None:
    async with api.connect(LOGIN) as host:
        process = await host.start(
            ["python", "serve.py", "--port", "8000"],
            env={"TOKEN": "s3cret"},
            cwd=PurePosixPath("/home/alice/work"),
        )
        [fake] = processes.processes

        assert process.run_dir == PurePosixPath(f"/tmp/ezhpcy-{process.run_id}")
        assert len(process.run_id) == 16
        assert fake.run_dir == login.local_path(process.run_dir)
        # Started by its launch script, which holds the command and its
        # environment; only the run directory's path is on a command line.
        assert ["setsid", "-f", "bash", f"{process.run_dir}/launch.sh"] in (
            login.commands_run
        )
        assert not any("s3cret" in " ".join(argv) for argv in login.commands_run)
        script = fake.launch_script
        assert "export TOKEN=s3cret\n" in script
        assert "cd -- /home/alice/work && exec python serve.py --port 8000\n" in script
        record = json.loads((fake.run_dir / "process.json").read_text("utf-8"))
        assert record["run_id"] == process.run_id
        assert record["pgid"] == fake.pgid == process.pgid
        assert record["executable"] == "python"
        assert host.status == (
            api.ProcessStatus(run_id=process.run_id, executable="python"),
        )
        assert process.exit_status is None

        await process.stop()

        assert not run_dir_exists(login, process)
        assert host.status == ()


async def test_a_process_that_exits_ends_only_its_handle(
    login: SSHTestServer, processes: FakeProcesses
) -> None:
    login.command("hostname", reply(stdout="login-1\n"))

    async with api.connect(LOGIN) as host:
        process = await host.start(["python", "train.py"])
        [fake] = processes.processes
        fake.write_stderr("epoch 1\nTraceback\nKeyError: 'x'\n")
        fake.exit(3)

        async with asyncio.timeout(WAIT_SECONDS):
            assert await process.wait() == 3

        assert process.exit_status == 3
        [status] = host.status
        assert isinstance(status, api.ProcessStatus)
        assert status.exit_status == 3
        # Its output stays until it is stopped.
        assert await process.stderr(lines=2) == "Traceback\nKeyError: 'x'\n"
        # The host serves on.
        assert await host.run(["hostname"]) == "login-1\n"
        await process.stop()

        # It had exited: no signal.
        assert fake.signals == []
        assert not run_dir_exists(login, process)
        with pytest.raises(api.CommandFailed):
            await process.stderr()


async def test_stopping_a_process_signals_its_group_and_reports_the_signal(
    login: SSHTestServer, processes: FakeProcesses
) -> None:
    async with api.connect(LOGIN) as host:
        process = await host.start(["sleep", "infinity"])
        await process.stop()
        # Once only.
        await process.stop()

    [fake] = processes.processes
    assert fake.signals == ["TERM"]
    assert signals(processes) == [(fake.pgid, "TERM")]
    # As bash reports a death by TERM.
    assert await process.wait() == 143
    assert not run_dir_exists(login, process)


async def test_a_process_that_outlives_term_is_killed(
    login: SSHTestServer, processes: FakeProcesses
) -> None:
    processes.ignore = {"TERM"}

    async with api.connect(LOGIN) as host:
        process = await host.start(["stubborn"])
        await process.stop()

    [fake] = processes.processes
    assert fake.signals == ["TERM", "KILL"]
    # As bash would report it, had it lived to.
    assert await process.wait() == 137
    assert not run_dir_exists(login, process)


async def test_an_empty_command_or_a_bad_variable_name_is_refused(
    login: SSHTestServer, processes: FakeProcesses
) -> None:
    async with api.connect(LOGIN) as host:
        with pytest.raises(ValueError, match="needs a command"):
            await host.start([])
        with pytest.raises(ValueError, match="not names of environment variables"):
            await host.start(["true"], env={"A=B": "c"})

    assert processes.processes == []


# Forwarded ports


async def test_a_forward_reaches_a_destination_the_host_reaches(
    login: SSHTestServer,
) -> None:
    port = login.listen(_echo)

    async with api.connect(LOGIN) as host:
        forward = await host.forward(api.TCPDestination("127.0.0.1", port))
        assert await asyncio.to_thread(round_trip, forward.port) == b"ping"
        [status] = host.status
        assert status == api.ForwardStatus(
            destination=api.TCPDestination("127.0.0.1", port),
            port=forward.port,
            connections=forward.connections,
        )

        await forward.close()

        assert host.status == ()
        with pytest.raises(ConnectionRefusedError):
            await asyncio.to_thread(round_trip, forward.port)


async def test_a_forward_survives_a_destination_that_refuses(
    login: SSHTestServer,
) -> None:
    async with api.connect(LOGIN) as host:
        forward = await host.forward(api.TCPDestination("127.0.0.1", unused_port()))

        # Each connection is closed, and no longer counted; the port serves on.
        for _ in range(2):
            assert await asyncio.to_thread(round_trip, forward.port) == b""
            assert forward.connections == 0


async def test_a_reverse_forward_listens_on_the_hosts_loopback(
    login: SSHTestServer,
) -> None:
    listener = await asyncio.start_server(_echo, "127.0.0.1", 0)
    local_port = listener.sockets[0].getsockname()[1]

    async with listener, api.connect(LOGIN) as host:
        forward = await host.reverse_forward(
            0, api.TCPDestination("127.0.0.1", local_port)
        )

        # Asked for on loopback, explicitly; the test server listens there, on
        # this machine.
        assert login.remote_listens == [("127.0.0.1", 0)]
        assert forward.port != 0
        assert forward.status.reverse
        assert await asyncio.to_thread(round_trip, forward.port) == b"ping"


# Closing with the host


async def test_leaving_a_hosts_block_closes_its_attachments_newest_first(
    login: SSHTestServer, processes: FakeProcesses
) -> None:
    port = login.listen(_echo)

    async with api.connect(LOGIN) as host:
        first = await host.start(["first"])
        forward = await host.forward(api.TCPDestination("127.0.0.1", port))
        second = await host.start(["second"])
        assert len(host.status) == 3

    pgids = [process.pgid for process in processes.processes]
    assert signals(processes) == [(pgids[1], "TERM"), (pgids[0], "TERM")]
    assert not run_dir_exists(login, first)
    assert not run_dir_exists(login, second)
    with pytest.raises(ConnectionRefusedError):
        await asyncio.to_thread(round_trip, forward.port)


# A job's node


class JobRig:
    """The login node, the worker's sshd, and a job's scheduler."""

    def __init__(
        self,
        login: SSHTestServer,
        worker: SSHTestServer,
        monkeypatch: pytest.MonkeyPatch,
        runtime: Path,
    ) -> None:
        self.login = login
        self.worker = worker
        self.runtime = runtime
        configure_tunnel(monkeypatch, login)
        quick_processes(monkeypatch)
        self.jobs: list[api.Job] = []
        self.schedulers = use_scheduler(
            monkeypatch,
            lambda host: FakeScheduler(
                host,
                [RUNNING],
                submitted=worker_ready_on(login, worker.port),
            ),
        )

    def job(self, host: api.Host) -> api.Job:
        return host.job(JOB, worker_ports=[self.worker.port])

    def host_block(self, alias: str) -> Path:
        return self.runtime / "ssh-config" / f"active-{alias}.conf"


@pytest.fixture
def job_rig(
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
) -> JobRig:
    return JobRig(login, worker, monkeypatch, isolated_runtime_dir)


async def test_a_jobs_host_is_its_node(job_rig: JobRig, tmp_path: Path) -> None:
    worker = job_rig.worker
    processes = FakeProcesses(worker)
    port = worker.listen(_echo)
    local = tmp_path / "params.yaml"
    local.write_text("cores: 4\n", encoding="utf-8")

    async with api.connect(LOGIN) as login_node, job_rig.job(login_node) as job:
        node = job.host
        assert node is job.host
        assert await node.run(["hostname"]) == f"{NODE}\n"
        await node.put(local, PurePosixPath("/home/alice/params.yaml"))
        process = await node.start(["python", "serve.py"])
        forward = await node.forward(api.TCPDestination("127.0.0.1", port))
        assert await asyncio.to_thread(round_trip, forward.port) == b"ping"
        assert [type(status) for status in job.status.attachments] == [
            api.ProcessStatus,
            api.ForwardStatus,
        ]
        node_closed = asyncio.ensure_future(node.closed)

    assert worker.local_path("/home/alice/params.yaml").exists()
    [fake] = processes.processes
    assert fake.signals == ["TERM"]
    assert not run_dir_exists(worker, process)
    # Closed with the job, not lost.
    assert await node_closed is None
    assert job_rig.login.remote_listens == []


async def test_leaving_a_job_stops_its_nodes_processes_before_its_host_blocks(
    job_rig: JobRig,
) -> None:
    blocks_when_stopped: list[bool] = []

    def on_signal(_process: FakeProcess, _signal: str) -> None:
        blocks_when_stopped.append(job_rig.host_block("gpu").exists())

    FakeProcesses(job_rig.worker, on_signal=on_signal)

    async with (
        api.reserve_alias("gpu") as gpu,
        api.connect(LOGIN) as login_node,
        job_rig.job(login_node) as job,
    ):
        await job.publish(gpu)
        await job.host.start(["python", "serve.py"])

    assert blocks_when_stopped == [True]
    assert not job_rig.host_block("gpu").exists()
    [scheduler] = job_rig.schedulers
    assert scheduler.cancelled == [JOB_ID]


async def test_losing_the_jobs_host_ends_the_job(job_rig: JobRig) -> None:
    job_rig.login.command("true", reply())

    async def lose_the_node(login_node: api.Host) -> None:
        async with job_rig.job(login_node):
            job_rig.worker.drop()
            async with asyncio.timeout(WAIT_SECONDS):
                await asyncio.Event().wait()

    async with api.connect(LOGIN) as login_node:
        with pytest.raises(api.JobFailed, match="worker SSH connection lost"):
            await lose_the_node(login_node)
        # The login node's connection is another matter.
        assert await login_node.run(["true"]) == ""

    [scheduler] = job_rig.schedulers
    assert scheduler.cancelled == [JOB_ID]
    assert control_dirs(job_rig.login) == []
