"""
`ezhpcy.api`'s apps: `Host.run_app` on a plain host and on a job's node,
provisioned there, started through EzHPCy's Pixi as a process, checked for
readiness, forwarded here, and stopped. Uses `ezhpcy.api` alone, with the
in-process SSH servers and the fakes of `tests.support`: the fake processes
play the app's start by serving its socket or port, and its exit by writing
its status.
"""

import asyncio
import json
import logging
import shlex
import urllib.request
from pathlib import Path, PurePosixPath

import pytest

from ezhpcy import api
from tests.support.api import JOB, LOGIN, JobRig
from tests.support.apps import MINIMAL_APP, AppRig, http_app, write_project
from tests.support.login import CACHE, REMOTE_STATE
from tests.support.sshserver import SSHTestServer
from tests.support.tunnel import JOB_ID, control_dirs, unused_port
from tests.support.worker import NODE, FakeProcess, FakeProcesses

WAIT_SECONDS = 5
APPS_ROOT = CACHE / "ezhpcy-apps"
LAB = MINIMAL_APP + (
    'run.env.JUPYTER_TOKEN = "{token}"\n'
    'run.env.JUPYTER_RUNTIME_DIR = "{run_dir}/jupyter"\n'
    'access.url = "http://127.0.0.1:{local_port}/lab?token={token}"\n'
    'access.status_hints = ["lab --port {local_port} --token {token}"]\n'
    'ready = { check = "http", path = "/api" }\n'
)
BOARD = """\
[ezhpcy]
schema = 1

[ezhpcy.app.board."v2.18"]
environment.pixi = "pixi.toml"
run.command = ["tensorboard", "--logdir", "{workdir}/runs", "--port", "{port}"]
endpoint.transport = "tcp"
ready.check = "tcp"
"""


@pytest.fixture
def rig(login: SSHTestServer, monkeypatch: pytest.MonkeyPatch) -> AppRig:
    return AppRig(login, monkeypatch)


def config_of(directory: Path, manifest: str = LAB) -> api.Config:
    manifest_path = write_project(directory / "project", manifest)
    config_file = directory / "ezhpcy.toml"
    config_file.write_text("", encoding="utf-8")
    return api.Config.load(config_file, manifests=[manifest_path])


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=WAIT_SECONDS) as response:
        return response.read()


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def launch_lines(process: FakeProcess) -> list[str]:
    return process.launch_script.splitlines()


async def test_an_app_runs_on_a_plain_host_and_is_served_here(
    rig: AppRig, tmp_path: Path
) -> None:
    app = config_of(tmp_path).app("lab")

    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app)
        [fake] = rig.processes.processes

        assert lab.app is app
        token = lab.url.rsplit("token=", 1)[1]
        assert lab.url == f"http://127.0.0.1:{lab.local_port}/lab?token={token}"
        assert len(token) == 32
        # Rendered as the URL is.
        assert lab.status_hints == (f"lab --port {lab.local_port} --token {token}",)
        assert await asyncio.to_thread(fetch, lab.url) == b"hello"
        assert lab.process.run_dir == PurePosixPath(f"/tmp/ezhpcy-{lab.process.run_id}")
        assert lab.forward.status.destination == api.SocketDestination(
            lab.process.run_dir / "app.sock"
        )
        assert host.status == (
            api.AppStatus(
                name="lab",
                version="v1.0",
                state=api.AppState.READY,
                run_id=lab.process.run_id,
                url=lab.url,
                status_hints=lab.status_hints,
            ),
        )
        # The token is in the launch script's environment, never on a command
        # line; the run directory is the script's own.
        assert not any(token in shlex.join(argv) for argv in rig.server.commands_run)
        lines = launch_lines(fake)
        assert f"export EZHPCY_TOKEN={token}" in lines
        assert f"export JUPYTER_TOKEN={token}" in lines
        assert 'export EZHPCY_RUN_DIR="$run_dir"' in lines
        assert 'export EZHPCY_SOCKET="$run_dir"/app.sock' in lines
        assert 'export JUPYTER_RUNTIME_DIR="$run_dir"/jupyter' in lines
        workdir = APPS_ROOT / "work" / "lab"
        assert f"export EZHPCY_WORKDIR={workdir}" in lines
        assert rig.server.local_path(workdir).is_dir()
        # Through EzHPCy's own Pixi, as installed, in the workdir.
        environment = rig.pixi.installs[0]
        command = (
            f"    cd -- {workdir} && exec env PIXI_HOME={REMOTE_STATE.pixi_home()} "
            f"PIXI_CACHE_DIR={APPS_ROOT / 'pixi-cache'} "
            f"{REMOTE_STATE.pixi_executable()} run --as-is --executable "
            f"--manifest-path {environment} --environment default -- "
            'jupyter lab --ServerApp.sock="$run_dir"/app.sock'
        )
        assert command in lines
        record = json.loads((fake.run_dir / "process.json").read_text("utf-8"))
        assert (record["app"], record["version"], record["transport"]) == (
            "lab",
            "v1.0",
            "socket",
        )

        await lab.stop()

        assert fake.signals == ["TERM"]
        assert not fake.run_dir.exists()
        assert host.status == ()
    # A plain host's: no job, so no control directory.
    assert control_dirs(rig.server) == []


async def test_a_tcp_app_listens_on_the_port_its_launch_script_picks(
    rig: AppRig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig.serve_port()
    port = rig.processes.ports[0]

    async with api.connect(LOGIN) as host:
        with caplog.at_level(logging.WARNING, logger="ezhpcy"):
            board = await host.run_app(config_of(tmp_path, BOARD).app("board"))
        [fake] = rig.processes.processes

        assert board.forward.status.destination == api.TCPDestination("127.0.0.1", port)
        lines = launch_lines(fake)
        assert 'export EZHPCY_PORT="$port"' in lines
        assert any(line.endswith(' --port "$port"') for line in lines)
        # Picked before the session ends, and said after the PID.
        assert lines.index("printf '%s\\n' \"$port\"") < lines.index(
            'exec </dev/null >"$run_dir/out" 2>"$run_dir/err"'
        )
        record = json.loads((fake.run_dir / "process.json").read_text("utf-8"))
        assert record["port"] == port
        assert await asyncio.to_thread(fetch, board.url) == b"hello"

    [warning] = warnings(caplog)
    assert "App board v2.18 listens on a TCP port" in warning
    assert "other users on its host can reach it" in warning


async def test_an_app_that_never_becomes_ready_fails_with_its_stderr(
    rig: AppRig, tmp_path: Path
) -> None:
    rig.app = http_app(status=503)
    manifest = LAB.replace(
        'ready = { check = "http", path = "/api" }',
        'ready = { check = "http", path = "/api", timeout_seconds = 0.3 }',
    )
    rig.processes = FakeProcesses(
        rig.server,
        on_start=lambda process: (
            process.write_stderr("starting\nTraceback\nOSError: no GPU\n"),
            rig.server.allow_socket(process.remote_run_dir / "app.sock", rig.app),
        ),
    )

    async with api.connect(LOGIN) as host:
        with pytest.raises(api.AppFailed) as failed:
            await host.run_app(config_of(tmp_path, manifest).app("lab"))

        message = str(failed.value)
        assert "app lab v1.0 did not become ready within 0.3 seconds" in message
        assert "GET /api answered 503" in message
        assert failed.value.stderr == "starting\nTraceback\nOSError: no GPU\n"
        # Stopped, and its run directory removed, once its stderr was read.
        [fake] = rig.processes.processes
        assert fake.signals == ["TERM"]
        assert not fake.run_dir.exists()
        assert host.status == ()


async def test_an_app_that_exits_while_starting_fails_at_once(
    rig: AppRig, tmp_path: Path
) -> None:
    rig.app = None

    def exit_soon(process: FakeProcess) -> None:
        process.write_stderr("ModuleNotFoundError: No module named 'jupyter'\n")
        process.exit(1)

    rig.processes = FakeProcesses(rig.server, on_start=exit_soon)

    async with api.connect(LOGIN) as host:
        async with asyncio.timeout(WAIT_SECONDS):
            with pytest.raises(api.AppFailed) as failed:
                await host.run_app(config_of(tmp_path).app("lab"))

        assert "app lab v1.0 exited with status 1 before it was ready" in str(
            failed.value
        )
        assert "No module named 'jupyter'" in failed.value.stderr
        [fake] = rig.processes.processes
        # It had exited: no signal.
        assert fake.signals == []
        assert not fake.run_dir.exists()


async def test_an_app_that_exits_while_serving_ends_only_its_handle(
    rig: AppRig, tmp_path: Path
) -> None:
    config = config_of(tmp_path)

    async with api.connect(LOGIN) as host:
        first = await host.run_app(config.app("lab"))
        second = await host.run_app(config.app("lab"))
        [fake, _other] = rig.processes.processes
        fake.exit(3)

        async with asyncio.timeout(WAIT_SECONDS):
            assert await first.wait() == 3

        assert first.status.state is api.AppState.EXITED
        assert first.status.exit_status == 3
        # The other run serves on.
        assert second.status.state is api.AppState.READY
        assert await asyncio.to_thread(fetch, second.url) == b"hello"
        await first.stop()
        assert [status.run_id for status in host.status] == [second.process.run_id]


async def test_another_live_run_of_the_app_is_noted(
    rig: AppRig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = config_of(tmp_path)

    async with api.connect(LOGIN) as host:
        first = await host.run_app(config.app("lab"))
        with caplog.at_level(logging.INFO, logger="ezhpcy"):
            second = await host.run_app(config.app("lab"))

        assert first.process.run_dir != second.process.run_dir
        [note] = [
            record.getMessage()
            for record in caplog.records
            if "Another run" in record.getMessage()
        ]
        assert f"lab is live on this host: v1.0 in {first.process.run_dir}" in note


async def test_leaving_the_hosts_block_stops_its_apps_newest_first(
    rig: AppRig, tmp_path: Path
) -> None:
    rig.serve_port()
    async with api.connect(LOGIN) as host:
        await host.run_app(config_of(tmp_path / "a").app("lab"))
        await host.run_app(config_of(tmp_path / "b", BOARD).app("board"))

    lab, board = rig.processes.processes
    signalled = [
        argv[6]
        for argv in rig.server.commands_run
        if argv[:2] == ["bash", "-c"] and argv[3:4] == ["stop"]
    ]
    assert signalled == [str(board.pgid), str(lab.pgid)]
    assert not lab.run_dir.exists()
    assert not board.run_dir.exists()


async def test_without_provisioning_an_app_must_be_installed(
    rig: AppRig, tmp_path: Path
) -> None:
    app = config_of(tmp_path).app("lab")

    async with api.connect(LOGIN) as host:
        with pytest.raises(api.ProvisioningError, match="isn't provisioned"):
            await host.run_app(app, provision=False)
        assert host.status == ()
        assert rig.processes.processes == []

        await (await host.run_app(app)).stop()
        installs = len(rig.pixi.installs)
        lab = await host.run_app(app, provision=False)

        assert len(rig.pixi.installs) == installs
        assert lab.status.state is api.AppState.READY


async def test_a_link_is_not_run(rig: AppRig, tmp_path: Path) -> None:
    manifest = '[ezhpcy]\nschema = 1\n[ezhpcy.app.code."v1.0"]\naccess.url = "x"\n'
    link = config_of(tmp_path, manifest).app("code")

    async with api.connect(LOGIN) as host:
        with pytest.raises(TypeError, match=r"code v1\.0 is a link"):
            await host.run_app(link)
        assert host.status == ()
        assert rig.processes.processes == []


async def test_the_apps_root_and_local_port_can_be_chosen(
    rig: AppRig, tmp_path: Path
) -> None:
    root = PurePosixPath("/work3/alice/apps")
    port = unused_port()

    async with api.connect(LOGIN) as host:
        lab = await host.run_app(
            config_of(tmp_path).app("lab"), apps_root=root, local_port=port
        )
        assert rig.pixi.installs[0].startswith(f"{root}/envs/")
        assert rig.server.local_path(root / "work" / "lab").is_dir()
        assert lab.local_port == port
        assert lab.url.startswith(f"http://127.0.0.1:{port}/lab?token=")
        await lab.stop()


async def test_an_app_on_a_jobs_node_is_provisioned_and_served_there(
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_rig = JobRig(login, worker, monkeypatch, isolated_runtime_dir)
    rig = AppRig(worker, monkeypatch)
    rig.serve_port()

    async with (
        api.connect(LOGIN) as login_node,
        job_rig.job(login_node, JOB.with_resources(exclusive=True)) as job,
    ):
        lab = await job.host.run_app(config_of(tmp_path / "a").app("lab"))
        with caplog.at_level(logging.WARNING, logger="ezhpcy"):
            await job.host.run_app(config_of(tmp_path / "b", BOARD).app("board"))

        assert await asyncio.to_thread(fetch, lab.url) == b"hello"
        assert [
            (status.name, status.state)
            for status in job.status.attachments
            if isinstance(status, api.AppStatus)
        ] == [("lab", api.AppState.READY), ("board", api.AppState.READY)]
        # Installed on the node, through its connection, not the login node's.
        assert len(rig.pixi.installs) == 1
        assert not any("install" in argv for argv in login.commands_run)

    # Even an exclusive job's node isn't counted on to be the user's alone.
    [warning] = warnings(caplog)
    assert "App board v2.18 listens on a TCP port" in warning
    # Stopped with the job.
    assert [fake.signals for fake in rig.processes.processes] == [["TERM"], ["TERM"]]


async def test_an_apps_exit_doesnt_end_its_job(
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
    tmp_path: Path,
) -> None:
    job_rig = JobRig(login, worker, monkeypatch, isolated_runtime_dir)
    rig = AppRig(worker, monkeypatch)

    async with (
        api.connect(LOGIN) as login_node,
        job_rig.job(login_node) as job,
    ):
        lab = await job.host.run_app(config_of(tmp_path).app("lab"))
        [fake] = rig.processes.processes
        fake.exit(1)
        async with asyncio.timeout(WAIT_SECONDS):
            assert await lab.wait() == 1

        assert job.status.phase is api.Phase.READY
        assert await job.host.run(["hostname"]) == f"{NODE}\n"

    [scheduler] = job_rig.schedulers
    assert scheduler.cancelled == [JOB_ID]


async def test_losing_the_nodes_connection_while_an_app_serves_ends_the_job(
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
    tmp_path: Path,
) -> None:
    job_rig = JobRig(login, worker, monkeypatch, isolated_runtime_dir)
    rig = AppRig(worker, monkeypatch)
    statuses: list[api.AppStatus] = []

    async def lose_the_node(login_node: api.Host) -> None:
        async with job_rig.job(login_node) as job:
            lab = await job.host.run_app(config_of(tmp_path).app("lab"))
            worker.drop()
            try:
                async with asyncio.timeout(WAIT_SECONDS):
                    await asyncio.Event().wait()
            finally:
                statuses.append(lab.status)

    async with api.connect(LOGIN) as login_node:
        with pytest.raises(api.JobFailed, match="worker SSH connection lost"):
            await lose_the_node(login_node)

    # It served until then; with its host gone, nothing could signal it, and
    # the scheduler's cancel ends it.
    assert [status.state for status in statuses] == [api.AppState.READY]
    [fake] = rig.processes.processes
    assert fake.signals == []
    [scheduler] = job_rig.schedulers
    assert scheduler.cancelled == [JOB_ID]


async def test_losing_the_node_while_waiting_on_an_app_blames_the_connection(
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
    tmp_path: Path,
) -> None:
    job_rig = JobRig(login, worker, monkeypatch, isolated_runtime_dir)
    AppRig(worker, monkeypatch)

    async def lose_the_node(login_node: api.Host) -> None:
        async with job_rig.job(login_node) as job:
            lab = await job.host.run_app(config_of(tmp_path).app("lab"))
            worker.drop()
            # Fails as soon as the connection goes, maybe before the job knows.
            async with asyncio.timeout(WAIT_SECONDS):
                await lab.wait()

    async with api.connect(LOGIN) as login_node:
        # The loss, not the wait that noticed it first.
        with pytest.raises(api.JobFailed, match="worker SSH connection lost"):
            await lose_the_node(login_node)
