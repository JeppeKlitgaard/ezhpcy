"""
Processes started detached (`ezhpcy.process`): the launch script, under bash
where there is one (in WSL on Windows), and finding a host's run directories.
The API's tests (`test_api_host.py`) cover the rest, with a fake for bash.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from ezhpcy import login as login_module, process
from ezhpcy.process import launch_script
from ezhpcy.types import Login
from tests.support.sshserver import SSHTestServer
from tests.support.worker import FakeProcesses, quick_processes

requires_bash = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
WAIT_SECONDS = 5


def test_the_launch_script_quotes_the_command_and_its_environment() -> None:
    script = launch_script(
        PurePosixPath("/tmp/ezhpcy-0123456789abcdef"),
        ["python", "-c", "print('a b')", "$HOME"],
        env={"TOKEN": "it's"},
        cwd=PurePosixPath("/home/alice/my work"),
    )

    assert "run_dir=/tmp/ezhpcy-0123456789abcdef\n" in script
    assert "export TOKEN='it'\"'\"'s'\n" in script
    assert (
        "cd -- '/home/alice/my work' && "
        "exec python -c 'print('\"'\"'a b'\"'\"')' '$HOME'\n"
    ) in script
    # Trapped before it says which group to signal.
    assert script.index("trap true HUP INT TERM\n") < script.index('"$$"')


async def test_list_runs_finds_the_records_of_the_hosts_run_directories(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    quick_processes(monkeypatch)
    FakeProcesses(login)
    # Another's, whose record can't be read, and a directory of another tool.
    login.local_path("/tmp/ezhpcy-ffffffffffffffff").mkdir()
    login.local_path("/tmp/other").mkdir()

    async with await login_module.connect(
        Login(user="alice", host="login.example.com")
    ) as host:
        started = [await process.start(host, ["sleep", "1"]) for _ in range(2)]
        runs = await process.list_runs(host)

    assert sorted(runs) == sorted(
        started_process.run_dir for started_process in started
    )
    assert {record["run_id"] for record in runs.values()} == {
        started_process.run_id for started_process in started
    }


# The launch script under bash


def _start(run_dir: Path, argv: list[str], **options: object) -> tuple[int, float]:
    """
    Run the launch script as `start` does, and return the process group it
    printed, and how long the launch took.
    """
    run_dir.mkdir(mode=0o700, exist_ok=bool(options.get("exist_ok")))
    (run_dir / "launch.sh").write_text(
        launch_script(
            PurePosixPath(run_dir),
            argv,
            env=options.get("env", {}),  # type: ignore[arg-type]
            cwd=options.get("cwd"),  # type: ignore[arg-type]
        ),
        encoding="utf-8",
    )
    began = time.monotonic()
    launched = subprocess.run(
        ["setsid", "-f", "bash", str(run_dir / "launch.sh")],
        capture_output=True,
        text=True,
        check=True,
        timeout=WAIT_SECONDS,
    )
    return int(launched.stdout), time.monotonic() - began


def _status(run_dir: Path) -> int:
    """Wait for the status file, and return the status in it."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not (status := run_dir / "status").exists():
        assert time.monotonic() < deadline, "no status file"
        time.sleep(0.01)
    return int(status.read_text(encoding="utf-8"))


@requires_bash
def test_the_launch_ends_once_the_script_has_printed_its_group(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "ezhpcy-run"

    pgid, took = _start(run_dir, ["sleep", "30"])

    # The session doesn't wait for the command.
    assert took < 2
    assert os.getpgid(pgid) == pgid
    assert not (run_dir / "status").exists()
    time.sleep(0.2)
    os.killpg(pgid, signal.SIGTERM)
    # The script outlives the signal, and reports the command's death by it.
    assert _status(run_dir) == 128 + signal.SIGTERM
    assert not (run_dir / "status.tmp").exists()


@requires_bash
def test_stopping_as_soon_as_the_group_is_known_still_stops_it(
    tmp_path: Path,
) -> None:
    """
    TERM may come before the command has started, which then misses it (it did
    in 29 of 30 tries without `stop`); the stop script's `stop` covers that.
    """
    for attempt in range(20):
        run_dir = tmp_path / f"ezhpcy-run-{attempt}"
        pgid, _took = _start(run_dir, ["sleep", "30"])
        subprocess.run(
            ["bash", "-c", process.STOP_SCRIPT, "stop", run_dir, "TERM", str(pgid)],
            check=True,
        )
        assert _status(run_dir) == 128 + signal.SIGTERM


@requires_bash
def test_a_process_stopped_before_it_began_never_runs(tmp_path: Path) -> None:
    run_dir = tmp_path / "ezhpcy-run"
    ran = tmp_path / "ran"
    run_dir.mkdir(mode=0o700)
    (run_dir / "stop").touch()

    _start(run_dir, ["touch", str(ran)], exist_ok=True)

    assert _status(run_dir) == 128 + signal.SIGTERM
    assert not ran.exists()


@requires_bash
def test_the_status_is_the_commands_and_its_output_is_in_the_run_directory(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "ezhpcy-run"

    _start(run_dir, ["sh", "-c", "echo out; echo err >&2; exit 3"])

    assert _status(run_dir) == 3
    assert (run_dir / "out").read_text(encoding="utf-8") == "out\n"
    assert (run_dir / "err").read_text(encoding="utf-8") == "err\n"


@requires_bash
def test_awkward_arguments_and_the_environment_reach_the_command_unchanged(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "ezhpcy-run"
    workdir = tmp_path / "my work"
    workdir.mkdir()
    awkward = ["a b", "\"q\" 's'", "$HOME", "*", "", "line\nbreak"]

    _start(
        run_dir,
        ["sh", "-c", 'printf "%s|" "$@" "$TOKEN" "$(pwd)"', "sh", *awkward],
        env={"TOKEN": "it's $secret"},
        cwd=PurePosixPath(workdir),
    )

    assert _status(run_dir) == 0
    printed = (run_dir / "out").read_text(encoding="utf-8").split("|")[:-1]
    assert printed == [*awkward, "it's $secret", str(workdir)]


@requires_bash
def test_a_failed_cd_is_the_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "ezhpcy-run"

    _start(run_dir, ["true"], cwd=PurePosixPath(tmp_path / "missing"))

    assert _status(run_dir) == 1
    assert "missing" in (run_dir / "err").read_text(encoding="utf-8")
