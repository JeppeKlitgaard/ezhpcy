"""
`ezhpcy tunnel --manifest PATH --app NAME`: the apps chosen before anything
else, then started on the job's node once the tunnel serves, in order, and
stopped first when it ends. An app that fails to start, or exits, leaves the
tunnel serving. Against the in-process SSH servers, the worker's sshd serving
the apps as `test_api_app.py`'s rig does.
"""

import asyncio
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path, PurePosixPath

import pytest

from ezhpcy import api
from ezhpcy.cli import tunnel as tunnel_cli
from tests.support.apps import MINIMAL_APP, AppRig, http_app, write_project
from tests.support.cli import Result, capture, invoke
from tests.support.login import remote_state_json
from tests.support.sshserver import SSHTestServer, reply
from tests.support.tunnel import configure_tunnel, serve_lsf_job, tunnel_log
from tests.support.worker import FakeProcess, FakeProcesses

WAIT_SECONDS = 5
APPS = (
    MINIMAL_APP
    + 'resources = { cores = 2, time_limit = "4:00" }\n'
    + """
[ezhpcy.app.board."v2.18"]
environment.pixi = "pixi.toml"
run.command = ["tensorboard", "--port", "{port}"]
endpoint.transport = "tcp"
resources.gpus = 1

[ezhpcy.app.board."v2.19rc1"]
environment.pixi = "pixi.toml"
run.command = ["tensorboard", "--port", "{port}"]
endpoint.transport = "tcp"
"""
)


@dataclass
class Serving:
    """What the tunnel did once its apps had started, seen from a thread."""

    run: tunnel_cli.TunnelRun
    started: list[api.RunningApp | None]
    loop: asyncio.AbstractEventLoop
    fetched: dict[str, bytes] = field(default_factory=dict)
    statuses: list[api.AppStatus] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)

    def app_statuses(self) -> list[api.AppStatus]:
        return [
            status
            for status in self.run.status.attachments
            if isinstance(status, api.AppStatus)
        ]


class TunnelApps:
    """
    A tunnel's job, as `serve_lsf_job` plays it, whose worker's sshd serves
    apps (`AppRig`), and the manifest `APPS` beside its Pixi files. `then`
    runs in a thread once every app given has started or failed to, and the
    job ends after it.
    """

    def __init__(
        self,
        login: SSHTestServer,
        worker: SSHTestServer,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self.worker = worker
        self.done = serve_lsf_job(login, monkeypatch, worker.port)
        # The node shares the login node's home directory.
        worker.command("bash -lc", reply(stdout=remote_state_json()))
        self.rig = AppRig(worker, monkeypatch)
        self.manifest = write_project(tmp_path / "project", APPS)
        self.then: Callable[[Serving], None] = lambda _serving: None
        self.serving: Serving | None = None
        self._error: BaseException | None = None
        start_app = tunnel_cli.TunnelRun.start_app
        started: list[api.RunningApp | None] = []

        async def recording(
            run: tunnel_cli.TunnelRun, host: api.Host, app: api.App
        ) -> api.RunningApp | None:
            running = await start_app(run, host, app)
            started.append(running)
            if len(started) == sum(isinstance(app, api.App) for app in run.apps):
                self.serving = Serving(run, list(started), asyncio.get_running_loop())
                threading.Thread(target=self._then, args=(self.serving,)).start()
            return running

        monkeypatch.setattr(tunnel_cli.TunnelRun, "start_app", recording)

    def _then(self, serving: Serving) -> None:
        try:
            self.then(serving)
        except BaseException as error:  # noqa: BLE001
            self._error = error
        finally:
            self.done.set()

    def tunnel(self, *arguments: str) -> Result:
        """Run the tunnel with `arguments`, and raise what `then` raised."""
        result = invoke(
            [
                "tunnel",
                "base",
                "--worker-port",
                str(self.worker.port),
                "--manifest",
                str(self.manifest),
                *arguments,
            ]
        )
        if self._error is not None:
            raise self._error
        return result


@pytest.fixture
def apps(
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> TunnelApps:
    return TunnelApps(login, worker, monkeypatch, tmp_path)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=WAIT_SECONDS) as response:
        return response.read()


def wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_the_apps_start_on_the_node_in_order_and_stop_with_the_tunnel(
    apps: TunnelApps,
) -> None:
    apps.rig.serve_port()

    def fetch_both(serving: Serving) -> None:
        for running in serving.started:
            assert running is not None
            serving.fetched[running.app.name] = fetch(running.url)
        serving.statuses = serving.app_statuses()
        # While their run directories are there.
        serving.launched = [
            process.launch_script for process in apps.rig.processes.processes
        ]

    apps.then = fetch_both

    with tunnel_log() as log:
        result = apps.tunnel("--app", "lab", "--app", "board")

    assert result.exit_code == 0, result
    serving = apps.serving
    assert serving is not None
    lab, board = serving.started
    assert lab is not None and board is not None
    assert serving.fetched == {"lab": b"hello", "board": b"hello"}
    # Each one's URL, on stdout, in order; the newest final version of each.
    assert result.stdout.splitlines() == [lab.url, board.url]
    assert [(status.name, status.version) for status in serving.statuses] == [
        ("lab", "v1.0"),
        ("board", "v2.18"),
    ]
    assert all(status.state is api.AppState.READY for status in serving.statuses)
    # Started in order, on the node, and stopped with the tunnel.
    first, second = serving.launched
    assert "-- jupyter lab" in first
    assert "-- tensorboard --port" in second
    assert [process.signals for process in apps.rig.processes.processes] == [
        ["TERM"],
        ["TERM"],
    ]
    assert apps.rig.pixi.installs
    assert any(
        message.startswith("App board v2.18 listens on a TCP port")
        for message in log.messages(logging.WARNING)
    )


def test_a_link_is_printed_for_the_alias_in_its_place(
    apps: TunnelApps, tmp_path: Path
) -> None:
    apps.manifest = write_project(
        tmp_path / "project",
        APPS
        + '[ezhpcy.app.code."v1.0"]\n'
        + 'access.url = "vscode://vscode-remote/ssh-remote+{alias}/"\n',
    )
    apps.rig.serve_port()

    result = apps.tunnel("--app", "code", "--app", "lab", "--alias", "node-1")

    assert result.exit_code == 0, result
    assert apps.serving is not None
    [lab] = apps.serving.started
    assert lab is not None
    assert result.stdout.splitlines() == [
        "vscode://vscode-remote/ssh-remote+node-1/",
        lab.url,
    ]
    # Nothing runs for it, and it is shown with the apps.
    assert len(apps.rig.processes.processes) == 1
    assert (
        api.LinkStatus(
            name="code", version="v1.0", url="vscode://vscode-remote/ssh-remote+node-1/"
        )
        in apps.serving.run.status.attachments
    )


def test_an_app_that_exits_leaves_the_tunnel_serving(apps: TunnelApps) -> None:
    def exit_then_serve_on(serving: Serving) -> None:
        [fake] = apps.rig.processes.processes
        fake.write_stderr("Traceback (most recent call last):\nMemoryError\n")
        fake.exit(3)
        wait_until(lambda: serving.app_statuses()[0].state is api.AppState.EXITED)
        serving.statuses = serving.app_statuses()
        wait_until(lambda: exit_message in log.messages(logging.ERROR))
        # Still serving: the job's own end ends it.
        assert serving.run.status.phase is api.Phase.READY

    exit_message = "App lab v1.0 exited with status 3. The tunnel keeps serving."
    apps.then = exit_then_serve_on

    with tunnel_log() as log:
        result = apps.tunnel("--app", "lab")

    assert result.exit_code == 0, result
    assert apps.serving is not None
    [exited] = apps.serving.statuses
    assert exited.exit_status == 3
    # Its stderr's end, shown with the error.
    assert "  MemoryError" in log.messages()
    assert (
        "Stopping job: worker job 42 ended in scheduler state DONE with exit code 0"
        in log.messages(logging.INFO)
    )


def test_an_app_that_fails_to_start_leaves_the_tunnel_serving_the_next(
    apps: TunnelApps,
) -> None:
    def fail_the_first(process: FakeProcess) -> None:
        if len(processes.processes) == 1:
            process.write_stderr("ModuleNotFoundError: No module named 'jupyter'\n")
            process.exit(1)

    processes = apps.rig.processes = FakeProcesses(apps.worker, on_start=fail_the_first)
    processes.ports.append(apps.worker.listen(http_app()))

    def note(serving: Serving) -> None:
        serving.statuses = serving.app_statuses()

    apps.then = note

    with tunnel_log() as log:
        result = apps.tunnel("--app", "lab", "--app", "board")

    assert result.exit_code == 0, result
    assert apps.serving is not None
    lab, board = apps.serving.started
    assert lab is None
    assert board is not None
    assert result.stdout.splitlines() == [board.url]
    # The one that failed keeps its row, after those attached.
    assert [
        (status.name, status.state, status.exit_status)
        for status in apps.serving.statuses
    ] == [("board", api.AppState.READY, None), ("lab", api.AppState.EXITED, None)]
    [error] = [
        message
        for message in log.messages(logging.ERROR)
        if message.startswith("Could not start app")
    ]
    assert error.startswith("Could not start app lab v1.0: app lab v1.0 exited with")
    assert error.endswith("The tunnel keeps serving.")


def test_ctrl_c_stops_the_apps_and_the_tunnel(apps: TunnelApps) -> None:
    def stop(serving: Serving) -> None:
        serving.loop.call_soon_threadsafe(serving.run.stop, t"Ctrl+C")
        # The job ends when it is cancelled, not before.
        wait_until(lambda: serving.run.status.phase is api.Phase.STOPPING)

    apps.then = stop

    with tunnel_log() as log:
        result = apps.tunnel("--app", "lab")

    assert result.exit_code == 0, result
    assert "Stopping job: Ctrl+C" in log.messages(logging.INFO)
    [fake] = apps.rig.processes.processes
    assert fake.signals == ["TERM"]
    assert not fake.run_dir.exists()


# Before connecting


@pytest.fixture
def settings(login: SSHTestServer, monkeypatch: pytest.MonkeyPatch) -> dict:
    return configure_tunnel(
        monkeypatch,
        login,
        resources={"queue": "hpc", "cores": 8, "memory": "32GB"},
        apps={"root": "/work3/alice/apps"},
    )


@pytest.fixture
def captured(settings: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    return capture(monkeypatch, "tunnel")


def test_the_apps_resources_are_over_the_profiles_and_the_clis_over_both(
    captured: dict, tmp_path: Path
) -> None:
    manifest = write_project(tmp_path / "project", APPS)

    result = invoke(
        ["tunnel", "base", "--manifest", str(manifest), "--app", "lab"]
        + ["--app", "board@v2.19rc1", "--app", "board", "--time-limit", "1:00"]
    )

    assert result.exit_code == 0, result
    assert [str(app) for app in captured["apps"]] == [
        "lab v1.0",
        "board v2.19rc1",
        "board v2.18",
    ]
    resources = captured["job"].resources
    assert (resources.queue, resources.cores, resources.gpus) == ("hpc", 2, 1)
    assert resources.memory_bytes == 32 * 10**9
    assert resources.time_limit == timedelta(hours=1)
    assert captured["apps_root"] == PurePosixPath("/work3/alice/apps")


def test_an_interactive_submission_command_decides_the_resources_alone(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_tunnel(monkeypatch, login)
    captured = capture(monkeypatch, "tunnel")
    manifest = write_project(tmp_path / "project", APPS)

    with tunnel_log() as log:
        result = invoke(
            ["tunnel", "base", "--manifest", str(manifest), "--app", "lab"]
            + ["--interactive-submission-command", "a100sh"]
        )

    assert result.exit_code == 0, result
    resources = captured["job"].resources
    assert (resources.cores, resources.time_limit) == (1, None)
    assert (
        "Ignoring the apps' resources: scheduler.interactive_submission_command "
        "decides them." in log.messages(logging.WARNING)
    )


def test_an_unknown_app_fails_before_connecting_listing_the_apps(
    login: SSHTestServer, captured: dict, tmp_path: Path
) -> None:
    manifest = write_project(tmp_path / "project", APPS)

    result = invoke(
        ["tunnel", "base", "--manifest", str(manifest), "--app", "nope"]
        + ["--no-load-builtin-manifests"]
    )

    assert result.exit_code == 2, result
    assert 'Invalid value "nope" for --app.' in result.stderr
    assert 'choose from: "board", "lab"' in result.stderr
    assert captured == {}
    assert login.auth_attempts == []


@pytest.mark.parametrize(
    ("app", "message"),
    [
        ("lab@v9", 'App "lab" has no version "v9"; choose from: "v1.0".'),
        ("lab@9", "for --app. invalid version '9'"),
    ],
)
def test_an_unknown_version_fails_listing_the_versions(
    captured: dict, tmp_path: Path, app: str, message: str
) -> None:
    manifest = write_project(tmp_path / "project", APPS)

    result = invoke(["tunnel", "base", "--manifest", str(manifest), "--app", app])

    assert result.exit_code == 2, result
    assert message in result.stderr
    assert captured == {}


def test_a_builtin_app_needs_no_manifest(captured: dict) -> None:
    result = invoke(["tunnel", "base", "--app", "lab", "--app", "vscode-remote"])

    assert result.exit_code == 0, result
    assert [str(app) for app in captured["apps"]] == [
        "lab v1.0",
        "vscode-remote v0.1.0",
    ]


def test_a_manifests_version_of_a_builtin_app_replaces_it(
    captured: dict, tmp_path: Path
) -> None:
    manifest = write_project(tmp_path / "project", APPS)

    result = invoke(["tunnel", "base", "--manifest", str(manifest), "--app", "lab"])

    assert result.exit_code == 0, result
    [lab] = captured["apps"]
    assert lab.environment.pixi.parent == tmp_path / "project"


@pytest.mark.parametrize(
    ("setting", "flags"),
    [(False, []), (True, ["--no-load-builtin-manifests"])],
    ids=["by-the-setting", "by-the-flag"],
)
def test_an_app_without_manifests_or_builtins_says_none_was_given(
    captured: dict, settings: dict, setting: bool, flags: list[str]
) -> None:
    settings["apps"] = {"load_builtin_manifests": setting}

    result = invoke(["tunnel", "base", "--app", "lab", *flags])

    assert result.exit_code == 2, result
    assert "There is no app" in result.stderr
    assert "no manifest with apps was given" in result.stderr
    assert captured == {}


def test_the_flag_loads_the_builtins_over_the_setting(
    captured: dict, settings: dict
) -> None:
    settings["apps"] = {"load_builtin_manifests": False}

    result = invoke(["tunnel", "base", "--app", "lab", "--load-builtin-manifests"])

    assert result.exit_code == 0, result
    assert [str(app) for app in captured["apps"]] == ["lab v1.0"]


def test_an_invalid_manifest_fails_before_connecting(
    captured: dict, tmp_path: Path
) -> None:
    manifest = write_project(tmp_path / "project", MINIMAL_APP + "rnu = 1\n")

    result = invoke(["tunnel", "base", "--manifest", str(manifest), "--app", "lab"])

    assert result.exit_code == 2, result
    assert "Invalid value for --manifest." in result.stderr
    assert "unknown key" in result.stderr
    assert captured == {}
