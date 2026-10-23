"""
The worker job running an app beside a stand-in sshd, under bash (Linux and
macOS only).
"""

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from socket import create_server

import pytest

from ezhpcy.worker.job import (
    APP_SCRIPT_NAME,
    RUN_ENV_NAME,
    WATCHDOG_SCRIPT,
    WORKER_SCRIPT,
)

requires_bash = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")

TOKEN = "0123456789abcdef0123456789abcdef"

# The worker job running the app, under bash


@dataclass
class Worker:
    """A worker job run by bash, with a stand-in sshd and cancel command."""

    process: subprocess.Popen[bytes]
    control_dir: Path
    logs_dir: Path
    run_dir: Path
    cancelled: Path

    def status(self, timeout: float = 10) -> str:
        """Wait for a status record, and return it without the token."""
        status = self.control_dir / "status"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if status.exists() and (record := status.read_text()):
                return record.removeprefix(f"{TOKEN} ").strip()
            time.sleep(0.05)
        raise AssertionError("no status")

    def wait_for(self, state: str, timeout: float = 15) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (record := self.status()).startswith(state):
                return record
            time.sleep(0.05)
        raise AssertionError(f"no {state} status, last {self.status()!r}")


@pytest.fixture
def stand_ins(tmp_path: Path) -> Path:
    """A directory on PATH with a stand-in `sshd` that just stays up."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sshd = bin_dir / "sshd"
    sshd.write_text("#!/bin/sh\nexec sleep 300\n")
    sshd.chmod(0o755)
    return bin_dir


@pytest.fixture
def start_worker(tmp_path: Path, stand_ins: Path) -> Generator[callable[..., Worker]]:  # type: ignore[valid-type]
    workers: list[Worker] = []

    def start(app_script_text: str, *, app_ports: str = "") -> Worker:
        control_dir = tmp_path / "control"
        control_dir.mkdir()
        run_dir = tmp_path / "ezhpcy-run"
        (control_dir / RUN_ENV_NAME).write_text(
            f"run_dir={shlex.quote(str(run_dir))}\napp_ports={app_ports}\n"
        )
        (control_dir / APP_SCRIPT_NAME).write_text(app_script_text)
        cancelled = tmp_path / "cancelled"
        cancel = tmp_path / "cancel"
        cancel.write_text(f'#!/bin/sh\necho "$@" >> "{cancelled}"\n')
        cancel.chmod(0o755)
        logs_dir = tmp_path / "logs"
        environment = {**os.environ, "PATH": f"{stand_ins}:{os.environ['PATH']}"}
        process = subprocess.Popen(
            ["bash", "-c", WORKER_SCRIPT, "sshd", "54321", TOKEN, "60"]
            + [str(logs_dir), str(control_dir), str(cancel)],
            env=environment,
        )
        worker = Worker(process, control_dir, logs_dir, run_dir, cancelled)
        workers.append(worker)
        return worker

    yield start
    for worker in workers:
        worker.process.kill()
        worker.process.wait()


@requires_bash
def test_the_worker_starts_the_app_and_reports_its_socket(
    start_worker, tmp_path: Path
) -> None:
    marker = tmp_path / "app-started"
    worker = start_worker(f'touch "{marker}"\nexec sleep 300\n')

    assert worker.wait_for("ready") == "ready 54321 socket"
    # The run directory exists, private to the user.
    assert worker.run_dir.is_dir()
    assert worker.run_dir.stat().st_mode & 0o777 == 0o700
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()


@requires_bash
def test_the_apps_exit_ends_the_job_and_removes_the_run_directory(
    start_worker,
) -> None:
    worker = start_worker("echo failing >&2\nsleep 0.5\nexit 3\n")

    assert worker.wait_for("ready") == "ready 54321 socket"
    assert worker.process.wait(timeout=20) == 3
    assert worker.status() == "stopped app 3"
    assert not worker.run_dir.exists()
    # Its stderr is in the logs, by the job's token.
    assert (worker.logs_dir / f"app-{TOKEN}.err").read_text() == "failing\n"


@requires_bash
def test_a_signal_stops_the_app_and_removes_the_run_directory(
    start_worker, tmp_path: Path
) -> None:
    child_pid = tmp_path / "child.pid"
    # The app starts a child of its own, as Jupyter starts kernels.
    worker = start_worker(f'sleep 300 &\necho $! > "{child_pid}"\nwait\n')
    worker.wait_for("ready")
    deadline = time.monotonic() + 5
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    # As the scheduler does when the job is cancelled.
    worker.process.terminate()

    assert worker.process.wait(timeout=20) == 143
    assert not worker.run_dir.exists()
    child = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child)


def _alive(pid: int) -> bool:
    """Whether `pid` runs: a zombie, waiting to be reaped, doesn't."""
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        check=False,
        text=True,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


@requires_bash
def test_an_existing_run_directory_fails_cleanly_and_is_kept(
    start_worker, tmp_path: Path
) -> None:
    other = tmp_path / "ezhpcy-run"
    other.mkdir()
    (other / "someone-elses").write_text("")

    worker = start_worker("exec sleep 300\n")

    assert worker.process.wait(timeout=20) == 1
    assert worker.status() == "failed app_start_failed"
    assert (other / "someone-elses").exists()


@requires_bash
def test_a_tcp_app_gets_the_first_free_candidate_port(start_worker) -> None:
    with create_server(("127.0.0.1", 0)) as probe:
        free = probe.getsockname()[1]
    with create_server(("127.0.0.1", 0)) as taken:
        worker = start_worker(
            "exec sleep 300\n", app_ports=f"{taken.getsockname()[1]}:{free}"
        )

        assert worker.wait_for("ready") == f"ready 54321 {free}"


@requires_bash
def test_the_watchdog_cancels_the_job_once_the_app_has_exited(
    tmp_path: Path,
) -> None:
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    (control_dir / "lease.00000000000000000001").write_text(f"{TOKEN} 1\n")
    cancel = tmp_path / "cancel"
    cancel.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/cancelled"\n')
    cancel.chmod(0o755)
    log = tmp_path / "heartbeat.log"
    # Detached, so that it is reaped once it exits, as the worker reaps the app.
    app_pid = subprocess.check_output(
        ["bash", "-c", "sleep 0.5 > /dev/null 2>&1 & echo $!"], text=True
    ).strip()
    with subprocess.Popen(["sleep", "60"]) as sshd:
        try:
            subprocess.run(
                ["bash", "-c", WATCHDOG_SCRIPT, "watchdog", TOKEN, "30", str(log)]
                + [str(control_dir), "42", str(cancel), str(sshd.pid), app_pid],
                check=True,
                timeout=30,
            )
        finally:
            sshd.kill()

    assert (tmp_path / "cancelled").read_text() == "42\n"
    assert "event=job_cancelled job=42 reason=app_exited" in log.read_text()
