"""
`ezhpcy.api`'s hosts: commands, files, processes started detached and forwarded
ports, on a plain host (`connect`) and on a job's node (`Job.host`), and how what
is attached to a host is closed with its connection. Uses `ezhpcy.api` alone,
with the in-process SSH servers and the fakes of `tests.support`.
"""

import asyncio
import json
import shlex
import socket
import stat
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from ezhpcy import api, process as process_module
from tests.support.api import LOGIN, JobRig, SlowPorts
from tests.support.scheduler import answer_batch
from tests.support.sshserver import Invocation, ReadHandler, SSHTestServer, reply
from tests.support.tunnel import JOB_ID, control_dirs, unused_port
from tests.support.worker import NODE, FakeProcess, FakeProcesses, quick_processes

pytestmark = pytest.mark.usefixtures("quiet_api")

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
        # A connection closed at once may be reset, aborted (on Windows), or no
        # longer connected, depending on when the other end's reset arrives. What
        # came back shows whether it was closed too soon.
        try:
            stream.sendall(payload)
            stream.shutdown(socket.SHUT_WR)
            while chunk := stream.recv(64 * 1024):
                received += chunk
        except OSError:
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


async def test_queues_describes_the_clusters_queues_with_the_sites_wrappers(
    login: SSHTestServer,
) -> None:
    commands: list[list[str]] = []
    responses = {
        "qstat -Bf": json.dumps({"pbs_version": "20.0.0", "Server": {"server1": {}}}),
        "qstat -Qf": json.dumps(
            {
                "Queue": {
                    "openq": {"enabled": "True", "started": "True"},
                    "closedq": {"enabled": "False", "started": "True"},
                }
            }
        ),
        "pbsnodes -a": json.dumps(
            {"nodes": {"n1": {"queue": "openq", "resources_available": {"ncpus": 8}}}}
        ),
        "^id -Gn": "users\n",
        "find -L": (
            "/site/bin/linuxsh:QUE=openq\n"
            "/site/bin/linuxsh:$QSUB -I -q $QUE\n"
            "--- PATH\n"
            "/site/bin/linuxsh\t/site/bin/linuxsh\n"
        ),
    }

    def login_shell(invocation: Invocation) -> None:
        [_, _, script] = shlex.split(invocation.argv[2])
        invocation.stdout.write(answer_batch(script, responses, commands))

    login.command("bash -lc", login_shell)
    config = api.JobConfig(
        scheduler=api.SchedulerSettings(
            type=api.SchedulerType.PBS, submission_mode=api.SubmissionMode.BATCH
        )
    )
    site = api.SiteConfig(
        wrapper_directories=(PurePosixPath("/site/bin"),),
        pbs_command_directory=PurePosixPath("/opt/pbs/bin"),
    )

    async with api.connect(LOGIN) as host:
        cluster = await host.queues(config, site=site, only_available_to_me=True)

    assert (cluster.name, cluster.user) == ("server1", "alice")
    [queue] = cluster.queues
    assert (queue.name, queue.hosts) == ("openq", ("n1",))
    assert queue.submissions[0] == api.Submission(
        interactive=True, command=PurePosixPath("/site/bin/linuxsh")
    )
    assert ["/opt/pbs/bin/qstat", "-Qf", "-F", "json"] in commands
    assert commands[-1][-2:] == ["scan", "/site/bin"]


async def test_put_and_get_copy_files(login: SSHTestServer, tmp_path: Path) -> None:
    local = tmp_path / "params.yaml"
    local.write_text("cores: 4\n", encoding="utf-8")
    remote = PurePosixPath("/home/alice/params.yaml")

    async with api.connect(LOGIN) as host:
        await host.put(local, remote)
        assert login.local_path(remote).read_text(encoding="utf-8") == "cores: 4\n"
        await host.get(remote, tmp_path / "back.yaml")

    assert (tmp_path / "back.yaml").read_text(encoding="utf-8") == "cores: 4\n"


async def test_put_and_get_raise_ssh_errors(
    login: SSHTestServer, tmp_path: Path
) -> None:
    local = tmp_path / "params.yaml"
    local.write_text("cores: 4\n", encoding="utf-8")

    async with api.connect(LOGIN) as host:
        with pytest.raises(api.SSHError):
            await host.put(local, PurePosixPath("/home/alice/no/such/dir/params.yaml"))
        with pytest.raises(api.SSHError):
            await host.get(PurePosixPath("/home/alice/missing"), tmp_path / "back")


REPOSITORY = PurePosixPath("/home/alice/repository")


def on_host(login: SSHTestServer, files: dict[str, str]) -> Path:
    """Write `files`, by path under `REPOSITORY`, on the host; returns its local path."""
    root = login.local_path(REPOSITORY)
    root.mkdir(exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def tree(root: Path) -> dict[str, str | None]:
    """Every file's content, and every directory as None, by path under `root`."""
    return {
        path.relative_to(root).as_posix(): (
            path.read_text(encoding="utf-8") if path.is_file() else None
        )
        for path in sorted(root.rglob("*"))
    }


def recording_reads(login: SSHTestServer, *relatives: str) -> list[str]:
    """The files of `relatives`, under `REPOSITORY`, as they are read."""
    reads: list[str] = []

    def recorder(relative: str) -> ReadHandler:
        async def record() -> None:
            reads.append(relative)

        return record

    for relative in relatives:
        login.before_read(REPOSITORY / relative, recorder(relative))
    return reads


def link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("this account can't create symbolic links")


async def test_download_copies_a_tree(login: SSHTestServer, tmp_path: Path) -> None:
    on_host(
        login,
        {
            "repository.toml": "r",
            "apps/lab/v1/ezhpcy-apps.toml": "lab",
            "apps/lab/v1/assets/deep/data.txt": "data",
            ".git/HEAD": "head",
        },
    )
    login.local_path(REPOSITORY / "empty").mkdir()
    local = tmp_path / "mirrors" / "repository"

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)

    assert tree(local) == {
        ".git": None,
        ".git/HEAD": "head",
        "apps": None,
        "apps/lab": None,
        "apps/lab/v1": None,
        "apps/lab/v1/assets": None,
        "apps/lab/v1/assets/deep": None,
        "apps/lab/v1/assets/deep/data.txt": "data",
        "apps/lab/v1/ezhpcy-apps.toml": "lab",
        "empty": None,
        "repository.toml": "r",
    }
    assert [path.name for path in local.parent.iterdir()] == ["repository"]


async def test_download_again_fetches_only_what_changed_and_removes_what_went(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"same.txt": "same", "changed.txt": "old", "gone.txt": "x"})
    local = tmp_path / "repository"
    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)
        (root / "changed.txt").write_text("newer", encoding="utf-8")
        (root / "gone.txt").unlink()
        (root / "added.txt").write_text("added", encoding="utf-8")
        reads = recording_reads(login, "same.txt", "changed.txt", "added.txt")

        await host.download(REPOSITORY, local)

    assert sorted(reads) == ["added.txt", "changed.txt"]
    assert tree(local) == {
        "added.txt": "added",
        "changed.txt": "newer",
        "same.txt": "same",
    }


async def test_a_failed_download_leaves_the_copy_as_it_was(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"a.txt": "a", "b.txt": "b"})
    local = tmp_path / "mirrors" / "repository"

    async def refuse() -> None:
        raise PermissionError("refused")

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)
        (root / "a.txt").write_text("newer a", encoding="utf-8")
        (root / "b.txt").write_text("newer b", encoding="utf-8")
        login.before_read(REPOSITORY / "b.txt", refuse)

        with pytest.raises(api.SSHError):
            await host.download(REPOSITORY, local)

    assert tree(local) == {"a.txt": "a", "b.txt": "b"}
    assert [path.name for path in local.parent.iterdir()] == ["repository"]


async def test_a_cancelled_download_leaves_the_copy_as_it_was(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"a.txt": "a"})
    local = tmp_path / "mirrors" / "repository"
    held = threading.Event()
    released = threading.Event()

    async def hold() -> None:
        held.set()
        await asyncio.to_thread(released.wait, WAIT_SECONDS)

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)
        (root / "a.txt").write_text("newer a", encoding="utf-8")
        login.before_read(REPOSITORY / "a.txt", hold)
        download = asyncio.create_task(host.download(REPOSITORY, local))
        try:
            assert await asyncio.to_thread(held.wait, WAIT_SECONDS)
            download.cancel()
        finally:
            # Closing the SFTP session waits for the server's answer.
            released.set()
        with pytest.raises(asyncio.CancelledError):
            await download

    assert tree(local) == {"a.txt": "a"}
    assert [path.name for path in local.parent.iterdir()] == ["repository"]


async def test_download_removes_what_a_killed_download_left(
    login: SSHTestServer, tmp_path: Path
) -> None:
    on_host(login, {"a.txt": "a"})
    local = tmp_path / "mirrors" / "repository"
    for leftover in (".repository.download-x1", ".repository.download-x2.old"):
        (local.parent / leftover / "sub").mkdir(parents=True)
    (local.parent / ".other.download-x3").mkdir()

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)

    assert sorted(path.name for path in local.parent.iterdir()) == [
        ".other.download-x3",
        "repository",
    ]


async def test_download_replaces_a_copy_of_read_only_files(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"shared/file.txt": "old"})
    local = tmp_path / "repository"
    (root / "shared" / "file.txt").chmod(0o444)
    (root / "shared").chmod(0o555)
    try:
        async with api.connect(LOGIN) as host:
            await host.download(REPOSITORY, local)
            (root / "shared").chmod(0o755)
            (root / "shared" / "file.txt").chmod(0o644)
            (root / "shared" / "file.txt").write_text("newer", encoding="utf-8")
            await host.download(REPOSITORY, local)
    finally:
        (root / "shared").chmod(0o755)
        (root / "shared" / "file.txt").chmod(0o644)

    assert tree(local) == {"shared": None, "shared/file.txt": "newer"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no executable bit")
async def test_download_keeps_only_the_executable_bit(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"run.sh": "#!/bin/sh\n", "data.txt": "data"})
    (root / "run.sh").chmod(0o700)
    (root / "data.txt").chmod(0o600)
    local = tmp_path / "repository"

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)

    assert stat.S_IMODE((local / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((local / "data.txt").stat().st_mode) == 0o644


async def test_download_copies_a_linked_directory_as_a_real_one(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"apps/placeholder": ""})
    elsewhere = login.local_path("/home/alice/elsewhere/lab")
    elsewhere.mkdir(parents=True)
    (elsewhere / "ezhpcy-apps.toml").write_text("lab", encoding="utf-8")
    link(root / "apps" / "lab", elsewhere)
    local = tmp_path / "repository"

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)

    assert not (local / "apps" / "lab").is_symlink()
    assert tree(local) == {
        "apps": None,
        "apps/lab": None,
        "apps/lab/ezhpcy-apps.toml": "lab",
        "apps/placeholder": "",
    }


async def test_download_skips_a_link_to_a_directory_above_it(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"apps/lab/ezhpcy-apps.toml": "lab"})
    link(root / "apps" / "lab" / "up", root / "apps")
    link(root / "itself", root)
    local = tmp_path / "repository"

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)

    assert tree(local) == {
        "apps": None,
        "apps/lab": None,
        "apps/lab/ezhpcy-apps.toml": "lab",
    }


async def test_download_copies_a_directory_at_every_path_fetching_it_once(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"apps/b/ezhpcy-apps.toml": "b"})
    link(root / "apps" / "a", root / "apps" / "b")
    link(root / ".hidden", root / "apps" / "b")
    reads = recording_reads(
        login,
        ".hidden/ezhpcy-apps.toml",
        "apps/a/ezhpcy-apps.toml",
        "apps/b/ezhpcy-apps.toml",
    )
    local = tmp_path / "repository"

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)

    assert len(reads) == 1
    assert tree(local) == {
        ".hidden": None,
        ".hidden/ezhpcy-apps.toml": "b",
        "apps": None,
        "apps/a": None,
        "apps/a/ezhpcy-apps.toml": "b",
        "apps/b": None,
        "apps/b/ezhpcy-apps.toml": "b",
    }


async def test_download_skips_a_broken_link(
    login: SSHTestServer, tmp_path: Path
) -> None:
    root = on_host(login, {"kept.txt": "kept"})
    link(root / "broken", root / "nowhere")
    local = tmp_path / "repository"

    async with api.connect(LOGIN) as host:
        await host.download(REPOSITORY, local)

    assert tree(local) == {"kept.txt": "kept"}


async def test_download_raises_ssh_error_for_a_missing_directory(
    login: SSHTestServer, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        with pytest.raises(api.SSHError):
            await host.download(PurePosixPath("/home/alice/missing"), tmp_path / "copy")

    assert not (tmp_path / "copy").exists()


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


async def test_a_reverse_forward_the_host_refuses_raises_an_ssh_error(
    login: SSHTestServer,
) -> None:
    login.refuses_listens = True

    async with api.connect(LOGIN) as host:
        with pytest.raises(api.SSHError):
            await host.reverse_forward(0, api.TCPDestination("127.0.0.1", 1))


# Cancelled midway


async def _cancelled(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _connects(port: int) -> bool:
    """
    Whether `port` takes a connection. One made as it closes may be reset, or
    accepted and never answered, so that isn't a refusal, nor worth waiting on.
    """
    try:
        # On Windows, a refusal takes about 2 seconds.
        with socket.create_connection(("127.0.0.1", port), WAIT_SECONDS):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True


async def _refused(port: int) -> None:
    """Wait for `port` to refuse connections: a host's may take a round trip."""
    async with asyncio.timeout(2 * WAIT_SECONDS):
        # ruff: ignore[ASYNC110]  # What it waits for changes on the server's thread
        while await asyncio.to_thread(_connects, port):
            await asyncio.sleep(0.01)


async def test_a_start_cancelled_before_its_process_runs_removes_its_run_directory(
    login: SSHTestServer,
    processes: FakeProcesses,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writing = asyncio.Event()

    async def wait_forever(*_args: object) -> None:
        writing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(process_module, "_write", wait_forever)

    async with api.connect(LOGIN) as host:
        starting = asyncio.create_task(host.start(["python", "serve.py"]))
        async with asyncio.timeout(WAIT_SECONDS):
            await writing.wait()
        await _cancelled(starting)

        assert host.status == ()
        assert processes.processes == []
        assert list(login.local_path("/tmp").iterdir()) == []


async def test_a_start_cancelled_while_its_process_starts_stops_it(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    starting_up = threading.Event()

    def slowly(_process: FakeProcess) -> None:
        starting_up.set()
        time.sleep(0.05)

    quick_processes(monkeypatch)
    processes = FakeProcesses(login, on_start=slowly)

    async with api.connect(LOGIN) as host:
        starting = asyncio.create_task(host.start(["python", "serve.py"]))
        await asyncio.to_thread(starting_up.wait, WAIT_SECONDS)
        await _cancelled(starting)

        [fake] = processes.processes
        assert fake.signals == ["TERM"]
        assert not fake.run_dir.exists()
        assert host.status == ()


async def test_a_forward_cancelled_midway_closes_its_port(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = SlowPorts(monkeypatch, "forward_local_port")

    async with api.connect(LOGIN) as host:
        forwarding = asyncio.create_task(
            host.forward(api.TCPDestination("127.0.0.1", login.listen(_echo)))
        )
        async with asyncio.timeout(WAIT_SECONDS):
            await ports.opened.wait()
        await _cancelled(forwarding)

        assert host.status == ()
        await _refused(ports.port)


async def test_a_reverse_forward_cancelled_midway_closes_its_port(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = SlowPorts(monkeypatch, "forward_remote_port")

    async with api.connect(LOGIN) as host:
        forwarding = asyncio.create_task(
            host.reverse_forward(0, api.TCPDestination("127.0.0.1", unused_port()))
        )
        async with asyncio.timeout(WAIT_SECONDS):
            await ports.opened.wait()
        await _cancelled(forwarding)

        assert host.status == ()
        await _refused(ports.port)


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
