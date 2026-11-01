"""
An app's actions: what the manifest may say of them, and through `ezhpcy.api`,
describing them, running them by hand and by timer, and stopping them with their
app. The fake processes play an action's run as they play an app's.
"""

import asyncio
import json
import logging
import shlex
import sys
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from ezhpcy import api
from ezhpcy.app.manifest import Manifest
from tests.support.api import LOGIN
from tests.support.apps import MINIMAL_APP, AppRig, write_project
from tests.support.login import CACHE
from tests.support.sshserver import SSHTestServer
from tests.support.worker import FakeProcess

WAIT_SECONDS = 5
LAB = 'ezhpcy.app.lab."v1.0"'
WORKDIR = CACHE / "ezhpcy-apps" / "work" / "lab"
ACTIONS = (
    MINIMAL_APP
    + """\
assets.include = ["script.py"]

[ezhpcy.app.lab."v1.0".action.reindex]
description = "Reindex the notebooks"
run.command = ["python", "{assets}/script.py", "--amount", "{amount}", "--to", "{socket}"]
run.env.TARGET = "{where}"
run.env.TOKEN = "{token}"

[ezhpcy.app.lab."v1.0".action.reindex.input.amount]
type = "integer"
label = "How many"
default = 3
min = 1
max = 10

[ezhpcy.app.lab."v1.0".action.reindex.input.where]
type = "path"
default = "{workdir}/index"

[ezhpcy.app.lab."v1.0".action.name]
run.command = ["echo", "{name}"]
input.name = { type = "string" }

[ezhpcy.app.lab."v1.0".action.choose]
run.command = ["echo", "{colour}", "{ratio}", "{flag}", "{target}"]
input.colour = { type = "choice", choices = ["red", "blue"], default = "red" }
input.ratio = { type = "number", min = 0, max = 1, default = 0.5 }
input.flag = { type = "boolean", default = false }
input.target = { type = "path", kind = "directory", must_exist = true, default = "/tmp" }
"""
)
ONLY_ON_A_TIMER = (
    MINIMAL_APP
    + """\
[ezhpcy.app.lab."v1.0".action.scan]
run.command = ["scan"]
trigger = { manual = false, every = 3600 }
"""
)
ON_A_TIMER = (
    MINIMAL_APP
    + """\
[ezhpcy.app.lab."v1.0".action.tick]
run.command = ["tick"]
trigger.every = 0.05
"""
)


@pytest.fixture
def rig(login: SSHTestServer, monkeypatch: pytest.MonkeyPatch) -> AppRig:
    return AppRig(login, monkeypatch)


def manifest_path(directory: Path, manifest: str) -> Path:
    return write_project(
        directory / "project", manifest, files={"script.py": "print()\n"}
    )


def app_of(directory: Path, manifest: str = ACTIONS) -> api.App:
    app = Manifest.load(manifest_path(directory, manifest)).apps["lab"]["v1.0"]
    assert isinstance(app, api.App)
    return app


def problems(directory: Path, addition: str) -> str:
    """Why the manifest with `addition`, an action's fields, doesn't load."""
    manifest = MINIMAL_APP + f'[ezhpcy.app.lab."v1.0".action.go]\n{addition}\n'
    with pytest.raises(api.ManifestError) as raised:
        Manifest.load(manifest_path(directory, manifest))
    return str(raised.value)


async def until(condition: Callable[[], object]) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        # ruff: ignore[ASYNC110]  # What it waits for changes on the server's thread
        while not condition():
            await asyncio.sleep(0.005)


async def next_process(rig: AppRig, count: int) -> FakeProcess:
    """The `count`th process on the host, once it has finished starting."""
    await until(lambda: rig.started >= count)
    return rig.processes.processes[count - 1]


def test_a_manifest_describes_actions_fully(tmp_path: Path) -> None:
    action = app_of(tmp_path).action["reindex"]

    assert action.description == "Reindex the notebooks"
    assert action.trigger.manual
    assert action.trigger.every is None
    amount = action.input["amount"]
    assert (amount.type, amount.label, amount.default) == ("integer", "How many", 3)
    assert (amount.min, amount.max) == (1, 10)
    assert action.input["where"].default == "{workdir}/index"


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ('run.command = ["x", "{nope}"]', "unknown placeholder {nope}"),
        ('run.command = ["{"]', "run.command[0]: invalid braces"),
        (
            'run.command = ["{run_dir}"]',
            "run.command[0]: action.run.command does not take {run_dir}",
        ),
        (
            'run.command = ["{token}"]',
            "run.command[0]: action.run.command does not take {token}",
        ),
        (
            'run.command = ["x"]\nrun.env.X = "{local_port}"',
            "run.env.X: action.run.env does not take {local_port}",
        ),
        (
            'run.command = ["{port}"]',
            "{port} is not set for endpoint.transport = 'socket'",
        ),
        ('run.command = ["{assets}"]', "{assets} is not set without [assets]"),
        (
            'run.command = ["x"]\nrun.env.EZHPCY_X = "y"',
            "EZHPCY_X is reserved for EzHPCy's own variables",
        ),
        (
            'run.command = ["x"]\ninput.a = { type = "string" }',
            "input.a: is not used by run.command or run.env",
        ),
        (
            'run.command = ["{socket}"]\ninput.socket = { type = "string" }',
            "'socket' is the name of a built-in placeholder",
        ),
        (
            'run.command = ["x"]\ninput.run_dir = { type = "string" }',
            "'run_dir' is the name of a built-in placeholder",
        ),
        (
            'run.command = ["x"]\ninput.a-b = { type = "string" }',
            "'a-b' can't be a placeholder's name",
        ),
        (
            'run.command = ["{a}"]\ntrigger.every = 60\ninput.a = { type = "string" }',
            "input 'a' needs a default",
        ),
        (
            'run.command = ["x"]\ntrigger.manual = false',
            "needs manual = true, or every",
        ),
        ('run.command = ["x"]\ntrigger.every = 0', "should be greater than"),
        ("description = 'no command'", "run: Field required"),
        (
            'run.command = ["{a}"]\ninput.a = { type = "string", min = 1 }',
            "min only apply to type = 'integer' or 'number'",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "string", choices = ["x"] }',
            "choices only apply to type = 'choice'",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "number", kind = "file" }',
            "kind only apply to type = 'path'",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "choice" }',
            "type = 'choice' needs choices",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "choice", choices = ["x", "x"] }',
            "choices must differ",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "integer", min = 2, max = 1 }',
            "min must not be more than max",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "integer", min = 0.5 }',
            "min and max of an integer must be integers",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "integer", default = "x" }',
            "default: 'x' is not an integer",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "integer", default = true }',
            "default: True is not an integer",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "integer", default = 5, max = 1 }',
            "default: 5 is more than the maximum, 1",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "choice", choices = ["x"], default = "y" }',
            "default: 'y' is none of ['x']",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "path", default = "relative" }',
            "input.a.default: must be an absolute path on the host",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "path", default = "{token}/x" }',
            "input.a.default: a path's default does not take {token}",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "path", default = "{assets}/x" }',
            "input.a.default: {assets} is not set without [assets]",
        ),
        (
            'run.command = ["{a}"]\ninput.a = { type = "path", default = "{workdir" }',
            "input.a.default: invalid braces",
        ),
    ],
)
def test_what_a_manifest_may_not_say_of_an_action(
    tmp_path: Path, addition: str, message: str
) -> None:
    found = problems(tmp_path, addition)

    assert message in found
    assert f"{LAB}.action.go" in found


def test_an_action_name_must_be_a_bare_key(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + (
        '[ezhpcy.app.lab."v1.0".action."a b"]\nrun.command = ["x"]\n'
    )

    with pytest.raises(api.ManifestError) as raised:
        Manifest.load(manifest_path(tmp_path, manifest))

    assert "invalid action name 'a b'" in str(raised.value)


def test_a_link_has_no_actions(tmp_path: Path) -> None:
    manifest = (
        '[ezhpcy]\nschema = 1\n\n[ezhpcy.app.site."v1"]\naccess.url = "http://x/"\n'
        '[ezhpcy.app.site."v1".action.go]\nrun.command = ["x"]\n'
    )

    with pytest.raises(api.ManifestError) as raised:
        Manifest.load(manifest_path(tmp_path, manifest))

    assert "action: unknown key" in str(raised.value)


@pytest.mark.parametrize(
    ("fields", "text", "value"),
    [
        ({"type": "string"}, "a b", "a b"),
        ({"type": "integer"}, " 12 ", 12),
        ({"type": "number"}, "1.5", 1.5),
        ({"type": "boolean"}, "Yes", True),
        ({"type": "boolean"}, "false", False),
        ({"type": "choice", "choices": ("x",)}, "x", "x"),
        ({"type": "path"}, "/a", "/a"),
    ],
)
def test_parsing_what_a_user_typed(
    fields: dict[str, object], text: str, value: object
) -> None:
    assert api.ActionInput.model_validate(fields).parse(text) == value


@pytest.mark.parametrize(
    ("kind", "text"),
    [("integer", "1.5"), ("integer", ""), ("number", "x"), ("boolean", "maybe")],
)
def test_parsing_what_a_user_mistyped(kind: str, text: str) -> None:
    with pytest.raises(ValueError, match="is not"):
        api.ActionInput.model_validate({"type": kind}).parse(text)


@pytest.mark.usefixtures("quiet_api")
async def test_an_action_runs_through_the_apps_environment_and_says_how_it_ended(
    rig: AppRig, tmp_path: Path
) -> None:
    app = app_of(tmp_path)

    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app)
        [fake_app] = rig.processes.processes
        running = asyncio.create_task(lab.run_action("reindex", amount=7))
        fake = await next_process(rig, 2)
        await until(lambda: host.status[0].actions)  # pyrefly: ignore[missing-attribute]
        [during] = host.status[0].actions  # pyrefly: ignore[missing-attribute]
        assert (during.action, during.state) == ("reindex", api.ActionState.RUNNING)
        assert during.finished is None

        lines = fake.launch_script.splitlines()
        [command] = [line for line in lines if "exec env PIXI_HOME" in line]
        assert command.startswith(f"    cd -- {WORKDIR} && exec env ")
        environment = rig.pixi.installs[0]
        # In the app's environment, reaching its socket, not a socket of its own.
        assert f"run --as-is --executable --manifest-path {environment}" in command
        assets = lab.actions["reindex"].input["where"]
        argv = shlex.split(command)
        assert argv[-4:] == ["--amount", "7", "--to", f"{lab.process.run_dir}/app.sock"]
        assert assets.default == f"{WORKDIR}/index"
        assert f"export TARGET={WORKDIR}/index" in lines
        assert f"export EZHPCY_SOCKET={lab.process.run_dir}/app.sock" in lines
        assert f"export EZHPCY_WORKDIR={WORKDIR}" in lines
        # The token too, as for the app: only in the environment.
        token = next(
            line.removeprefix("export TOKEN=")
            for line in lines
            if line.startswith("export TOKEN=")
        )
        assert f"export EZHPCY_TOKEN={token}" in fake_app.launch_script.splitlines()
        assert not any(token in shlex.join(ran) for ran in rig.server.commands_run)
        # Not its own values, which belong to the app.
        assert not any(line.startswith("export EZHPCY_RUN_DIR") for line in lines)
        record = json.loads((fake.run_dir / "process.json").read_text("utf-8"))
        assert "app" not in record
        assert (record["action"], record["action_of"]) == ("reindex", "lab")

        fake.write_stdout("first\nsecond\n")
        fake.write_stderr("noise\n")
        fake.exit(0)
        result = await running

        assert result.action == "reindex"
        assert result.state is api.ActionState.SUCCEEDED
        assert result.exit_status == 0
        assert result.stdout == "first\nsecond\n"
        # Only when it failed.
        assert result.stderr == ""
        assert result.finished is not None
        assert result.finished >= result.started
        assert host.status[0].actions == (result,)  # pyrefly: ignore[missing-attribute]
        assert not fake.run_dir.exists()


@pytest.mark.usefixtures("quiet_api")
async def test_an_actions_failure_is_a_result_with_its_stderr(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        running = asyncio.create_task(lab.run_action("reindex"))
        fake = await next_process(rig, 2)
        fake.write_stdout("partly\n")
        fake.write_stderr("it broke\n")
        fake.exit(3)

        result = await running

        assert result.state is api.ActionState.FAILED
        assert result.exit_status == 3
        assert result.stdout == "partly\n"
        assert result.stderr == "it broke\n"
        assert lab.status.state is api.AppState.READY


@pytest.mark.usefixtures("quiet_api")
async def test_an_input_may_be_called_name(rig: AppRig, tmp_path: Path) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        running = asyncio.create_task(lab.run_action("name", name="$(rm -rf /)"))
        fake = await next_process(rig, 2)

        # Quoted for the launch script's bash, and no shell after that.
        assert fake.launch_script.count("echo '$(rm -rf /)'") == 1
        fake.exit(0)
        await running


@pytest.mark.parametrize(
    ("action", "inputs", "at_fault", "message"),
    [
        ("nope", {}, None, "has no action"),
        ("reindex", {"nope": 1}, "nope", "has no input"),
        ("name", {}, "name", "needs a value for input"),
        ("reindex", {"amount": "3"}, "amount", "'3' is not an integer"),
        ("reindex", {"amount": True}, "amount", "True is not an integer"),
        ("reindex", {"amount": 11}, "amount", "more than the maximum"),
        ("reindex", {"amount": 0}, "amount", "less than the minimum"),
        ("choose", {"ratio": 2}, "ratio", "more than the maximum"),
        ("choose", {"ratio": float("nan")}, "ratio", "nan is not a number"),
        ("choose", {"flag": "true"}, "flag", "is not true or false"),
        ("choose", {"colour": "green"}, "colour", "is none of"),
        ("reindex", {"where": "relative"}, "where", "is not an absolute path"),
        ("reindex", {"where": 3}, "where", "is not a path"),
        ("reindex", {"where": "/a\0b"}, "where", "must not contain NUL"),
        ("name", {"name": "\0run_dir\0"}, "name", "must not contain NUL"),
        ("name", {"name": 3}, "name", "is not a string"),
    ],
)
@pytest.mark.usefixtures("quiet_api")
async def test_what_an_action_is_given_is_checked_before_it_runs(
    rig: AppRig,
    tmp_path: Path,
    action: str,
    inputs: dict[str, object],
    at_fault: str | None,
    message: str,
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))

        with pytest.raises(api.ActionError, match=message) as raised:
            await lab.run_action(action, **inputs)

        assert raised.value.input_name == at_fault
        assert len(rig.processes.processes) == 1
        assert lab.status.actions == ()


@pytest.mark.usefixtures("quiet_api")
async def test_an_action_only_on_a_timer_cant_be_run_by_hand(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path, ONLY_ON_A_TIMER))

        with pytest.raises(api.ActionError, match="runs only by its timer"):
            await lab.run_action("scan")

        (await next_process(rig, 2)).exit(0)


@pytest.mark.usefixtures("quiet_api")
async def test_a_path_is_checked_on_the_host(rig: AppRig, tmp_path: Path) -> None:
    (rig.server.local_path("/tmp") / "file").write_text("x", encoding="utf-8")

    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))

        with pytest.raises(api.ActionError, match="doesn't exist on the host"):
            await lab.run_action("choose", target="/tmp/nope")
        with pytest.raises(api.ActionError, match="isn't a directory on the host"):
            await lab.run_action("choose", target="/tmp/file")
        assert len(rig.processes.processes) == 1

        running = asyncio.create_task(
            lab.run_action("choose", target=PurePosixPath("/tmp"))
        )
        fake = await next_process(rig, 2)
        assert "echo red 0.5 false /tmp" in fake.launch_script
        fake.exit(0)
        assert (await running).state is api.ActionState.SUCCEEDED


@pytest.mark.usefixtures("quiet_api")
async def test_an_action_runs_once_at_a_time(rig: AppRig, tmp_path: Path) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        running = asyncio.create_task(lab.run_action("reindex"))
        fake = await next_process(rig, 2)

        with pytest.raises(api.ActionError, match="is running already"):
            await lab.run_action("reindex")

        fake.exit(0)
        await running
        again = asyncio.create_task(lab.run_action("reindex"))
        (await next_process(rig, 3)).exit(0)
        await again


@pytest.mark.usefixtures("quiet_api")
async def test_an_action_needs_its_app_running(rig: AppRig, tmp_path: Path) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        [fake] = rig.processes.processes
        fake.exit(1)
        await lab.wait()

        with pytest.raises(api.ActionError, match=r"needs app lab v1\.0 running"):
            await lab.run_action("reindex")

        await lab.stop()
        with pytest.raises(api.ActionError, match=r"needs app lab v1\.0 running"):
            await lab.run_action("reindex")


@pytest.mark.usefixtures("quiet_api")
async def test_cancelling_a_run_stops_its_process(rig: AppRig, tmp_path: Path) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        running = asyncio.create_task(lab.run_action("reindex"))
        fake = await next_process(rig, 2)
        await until(lambda: lab.status.actions)

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        assert fake.signals == ["TERM"]
        assert not fake.run_dir.exists()
        [run] = lab.status.actions
        assert run.state is api.ActionState.FAILED
        assert lab.status.state is api.AppState.READY
        # It can run again.
        again = asyncio.create_task(lab.run_action("reindex"))
        (await next_process(rig, 3)).exit(0)
        await again


@pytest.mark.usefixtures("quiet_api")
async def test_stopping_the_app_stops_its_actions_first(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        [fake_app] = rig.processes.processes
        running = asyncio.create_task(lab.run_action("reindex"))
        fake = await next_process(rig, 2)

        await lab.stop()

        assert fake.signals == ["TERM"]
        assert fake_app.signals == ["TERM"]
        assert not fake.run_dir.exists()
        assert not fake_app.run_dir.exists()
        result = await running
        assert result.state is api.ActionState.FAILED
        assert result.exit_status == 143


@pytest.mark.usefixtures("quiet_api")
async def test_stopping_the_app_stops_an_action_that_is_still_starting(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        release = rig.hold_starts = threading.Event()
        running = asyncio.create_task(lab.run_action("reindex"))
        await until(lambda: len(rig.processes.processes) == 2)

        # Started on the host, and the client hasn't heard yet.
        stopping = asyncio.create_task(lab.stop())
        await asyncio.sleep(0.05)
        assert rig.started == 1
        assert not stopping.done()
        release.set()
        await stopping

        fake = rig.processes.processes[1]
        assert fake.signals == ["TERM"]
        assert not fake.run_dir.exists()
        assert (await running).exit_status == 143


@pytest.mark.usefixtures("quiet_api")
async def test_leaving_the_connection_stops_an_action(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path))
        running = asyncio.create_task(lab.run_action("reindex"))
        fake = await next_process(rig, 2)

    assert fake.signals == ["TERM"]
    assert not fake.run_dir.exists()
    assert (await running).exit_status == 143


@pytest.mark.usefixtures("quiet_api")
async def test_a_timer_runs_at_once_and_not_again_before_its_interval(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path, ONLY_ON_A_TIMER))

        # Its interval is an hour, so this is its first run.
        fake = await next_process(rig, 2)
        fake.exit(0)
        await until(lambda: lab.status.actions and lab.status.actions[0].finished)
        await asyncio.sleep(0.05)

        assert len(rig.processes.processes) == 2


@pytest.mark.usefixtures("quiet_api")
async def test_a_timer_runs_after_each_interval_and_skips_while_running(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path, ON_A_TIMER))
        fake = await next_process(rig, 2)
        # Longer than several intervals, and it has only the one run.
        await asyncio.sleep(0.2)
        assert len(rig.processes.processes) == 2

        fake.write_stdout("done\n")
        fake.exit(0)
        following = await next_process(rig, 3)
        [run] = lab.status.actions
        assert run.action == "tick"
        following.exit(0)

        await lab.stop()
        count = len(rig.processes.processes)
        await asyncio.sleep(0.2)
        assert len(rig.processes.processes) == count


async def test_a_timer_continues_after_a_failure_and_says_so(
    rig: AppRig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with api.connect(LOGIN) as host:
        await host.run_app(app_of(tmp_path, ON_A_TIMER))
        with caplog.at_level(logging.WARNING, logger="ezhpcy"):
            fake = await next_process(rig, 2)
            fake.write_stderr("first line\nit broke\n")
            fake.exit(3)
            following = await next_process(rig, 3)
            following.exit(0)

    # What stopping does to the last run isn't a failure to report.
    assert not any("status 143" in record.getMessage() for record in caplog.records)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Action tick of app lab v1.0 exited with status 3: it broke" in message
        and "Its timer continues" in message
        for message in messages
    )


@pytest.mark.usefixtures("quiet_api")
async def test_a_timers_run_logs_what_it_wrote(
    rig: AppRig, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path, ON_A_TIMER))
        with caplog.at_level(logging.INFO, logger="ezhpcy"):
            fake = await next_process(rig, 2)
            fake.write_stdout("used 3 GB\nof 30 GB\n")
            fake.exit(0)
            await until(
                lambda: any(r.getMessage() == "  of 30 GB" for r in caplog.records)
            )
        await lab.stop()

    messages = [record.getMessage() for record in caplog.records]
    at = messages.index("Action tick of app lab v1.0 ran by its timer.")
    assert messages[at + 1 : at + 3] == ["  used 3 GB", "  of 30 GB"]


@pytest.mark.usefixtures("quiet_api")
async def test_a_timer_stops_once_its_app_has_exited(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path, ON_A_TIMER))
        first_run = await next_process(rig, 2)
        rig.processes.processes[0].exit(1)
        await lab.wait()
        first_run.exit(0)
        await until(lambda: lab.status.actions and lab.status.actions[0].finished)

        count = len(rig.processes.processes)
        await asyncio.sleep(0.2)

        assert len(rig.processes.processes) == count


@pytest.mark.usefixtures("quiet_api")
async def test_a_process_output_can_be_read(rig: AppRig) -> None:
    async with api.connect(LOGIN) as host:
        process = await host.start(["x"])
        [fake] = rig.processes.processes
        fake.write_stdout("one\ntwo\nthree\n")

        assert await process.stdout(2) == "two\nthree\n"


@pytest.mark.usefixtures("quiet_api")
async def test_a_directory_can_be_listed(rig: AppRig) -> None:
    root = rig.server.local_path("/data")
    (root / "sub").mkdir(parents=True)
    (root / "b.txt").write_text("four", encoding="utf-8")

    async with api.connect(LOGIN) as host:
        entries = await host.list_directory(PurePosixPath("/data"))

    assert [(e.name, e.path, e.kind) for e in entries] == [
        ("b.txt", PurePosixPath("/data/b.txt"), "file"),
        ("sub", PurePosixPath("/data/sub"), "directory"),
    ]
    assert entries[0].size == 4


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges")
@pytest.mark.usefixtures("quiet_api")
async def test_a_listing_follows_symbolic_links(rig: AppRig) -> None:
    root = rig.server.local_path("/data")
    (root / "sub").mkdir(parents=True)
    (root / "to-sub").symlink_to(root / "sub")
    (root / "broken").symlink_to(root / "nowhere")

    async with api.connect(LOGIN) as host:
        entries = await host.list_directory(PurePosixPath("/data"))

    assert {e.name: e.kind for e in entries} == {
        "broken": "other",
        "sub": "directory",
        "to-sub": "directory",
    }


@pytest.mark.usefixtures("quiet_api")
async def test_listing_what_cant_be_read_raises_ssh_error(rig: AppRig) -> None:
    async with api.connect(LOGIN) as host:
        with pytest.raises(api.SSHError):
            await host.list_directory(PurePosixPath("/nowhere"))


ACTIONS_ALONE = """\
[ezhpcy]
schema = 1

[ezhpcy.app.lab."v1.0"]
environment.pixi = "pixi.toml"
assets.include = ["script.py"]

[ezhpcy.app.lab."v1.0".action.go]
run.command = ["python", "{assets}/script.py", "{workdir}"]
"""
NOTHING_AT_ALL = """\
[ezhpcy]
schema = 1

[ezhpcy.app.lab."v1.0"]
environment.pixi = "pixi.toml"
"""


def test_an_app_may_be_its_actions_alone(tmp_path: Path) -> None:
    app = app_of(tmp_path, ACTIONS_ALONE)

    assert app.run is None
    assert app.endpoint is None


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ('run.command = ["x"]', "run and endpoint go together"),
        ('endpoint.transport = "socket"', "run and endpoint go together"),
        ("", "needs run and endpoint, or an action"),
        (
            'ready.check = "tcp"\n[ezhpcy.app.lab."v1.0".action.go]\nrun.command = ["x"]',
            "ready only apply with run and endpoint",
        ),
        (
            'access.url = "http://x/"\n[ezhpcy.app.lab."v1.0".action.go]\nrun.command = ["x"]',
            "access only apply with run and endpoint",
        ),
    ],
)
def test_what_an_app_without_a_server_may_not_say(
    tmp_path: Path, addition: str, message: str
) -> None:
    with pytest.raises(api.ManifestError) as raised:
        Manifest.load(manifest_path(tmp_path, f"{NOTHING_AT_ALL}{addition}\n"))

    assert message in str(raised.value)


@pytest.mark.parametrize(
    ("field", "name"),
    [("run.command", "socket"), ("run.command", "port"), ("run.env.X", "token")],
)
def test_an_app_without_a_server_has_none_of_its_placeholders(
    tmp_path: Path, field: str, name: str
) -> None:
    command = '"x"' if field == "run.env.X" else f'"{{{name}}}"'
    env = f'run.env.X = "{{{name}}}"\n' if field == "run.env.X" else ""
    manifest = (
        NOTHING_AT_ALL
        + f'[ezhpcy.app.lab."v1.0".action.go]\nrun.command = [{command}]\n{env}'
    )

    with pytest.raises(api.ManifestError) as raised:
        Manifest.load(manifest_path(tmp_path, manifest))

    assert f"{{{name}}} is not set without run and endpoint" in str(raised.value)


@pytest.mark.usefixtures("quiet_api")
async def test_an_app_of_actions_alone_is_ready_without_any_process(
    rig: AppRig, tmp_path: Path
) -> None:
    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path, ACTIONS_ALONE))

        assert rig.processes.processes == []
        # Its environment is installed, as for any app.
        assert rig.pixi.installs
        assert (lab.url, lab.process, lab.forward, lab.local_port) == (
            None,
            None,
            None,
            None,
        )
        assert lab.status.state is api.AppState.READY
        assert lab.status.run_id is not None
        assert host.status == (lab.status,)
        assert await lab.stderr() == ""
        assert await lab.log_stderr() == ""

        running = asyncio.create_task(lab.run_action("go"))
        fake = await next_process(rig, 1)
        lines = fake.launch_script.splitlines()
        assert f"export EZHPCY_WORKDIR={WORKDIR}" in lines
        assert not any("EZHPCY_SOCKET" in line for line in lines)
        assert not any("EZHPCY_PORT" in line for line in lines)
        assert not any("EZHPCY_TOKEN" in line for line in lines)
        [command] = [line for line in lines if "exec env PIXI_HOME" in line]
        assert command.startswith(f"    cd -- {WORKDIR} && exec env ")
        fake.write_stdout("done\n")
        fake.exit(0)
        result = await running
        assert result.stdout == "done\n"
        assert lab.status.actions == (result,)

        waiting = asyncio.create_task(lab.wait())
        await asyncio.sleep(0)
        assert not waiting.done()
        await lab.stop()
        assert await waiting == 0
        assert host.status == ()
        with pytest.raises(api.ActionError, match="running, and it isn't"):
            await lab.run_action("go")


@pytest.mark.usefixtures("quiet_api")
async def test_stopping_an_app_of_actions_alone_stops_its_timer_and_runs(
    rig: AppRig, tmp_path: Path
) -> None:
    manifest = ACTIONS_ALONE + "trigger.every = 0.05\n"

    async with api.connect(LOGIN) as host:
        lab = await host.run_app(app_of(tmp_path, manifest))
        fake = await next_process(rig, 1)

        await lab.stop()

        assert fake.signals == ["TERM"]
        count = len(rig.processes.processes)
        await asyncio.sleep(0.2)
        assert len(rig.processes.processes) == count


def test_the_quota_builtin_is_its_action_alone(tmp_path: Path) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text("", encoding="utf-8")

    app = api.Config.load(config_file).app("zhome-quota")

    assert isinstance(app, api.App)
    assert app.run is None
    assert app.action["quota"].trigger.every is not None
