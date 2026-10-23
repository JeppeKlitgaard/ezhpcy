"""
The worker job's command, its lease watchdog, and its control directory over
real SFTP.
"""

import asyncio
import subprocess
import sys
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

from ezhpcy import ssh
from ezhpcy.constants import EZHPCY_VERSION, OPENSSH_MATCHSPEC, PIXI_VERSION
from ezhpcy.types import Login, RemoteState
from ezhpcy.worker import WorkerError
from ezhpcy.worker.control import (
    WorkerControl,
    WorkerFailed,
    WorkerReady,
    WorkerStatus,
    WorkerStopped,
)
from ezhpcy.worker.job import (
    WATCHDOG_SCRIPT,
    WORKER_SCRIPT,
    select_ports,
    sshd_command,
)
from ezhpcy.worker.sshd import client_alive, sshd_config_arguments
from tests.support.sshserver import SSHTestServer

_CONTROL_DIR = PurePosixPath("/home/alice/state/worker_control/LEASETOKEN")


def test_sshd_command_retries_ports_through_pixi() -> None:
    remote_state = RemoteState(
        cache_dir=PurePosixPath("/home/alice/.cache"),
        state_dir=PurePosixPath("/home/alice/.local/state"),
    )
    command = sshd_command(
        remote_state,
        ports=(54321, 54322),
        heartbeat_token="LEASETOKEN",
        heartbeat_timeout_seconds=90,
        control_dir=PurePosixPath("/home/alice/.cache/ezhpcy/control/test"),
        cancel_executable="/opt/pbspro/bin/qdel",
        sshd_arguments=sshd_config_arguments(
            host_key=PurePosixPath("/remote/ssh_host_ed25519_key"),
            remote_username="alice",
            authorized_key=("ssh-ed25519", "WORKERKEY"),
        ),
    )

    assert command[:6] == (
        "env",
        f"PIXI_HOME=/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}",
        (
            f"PIXI_CACHE_DIR=/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi_cache/"
            f"{PIXI_VERSION}"
        ),
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi",
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
    )
    assert command[6:10] == ("bash", "-c", WORKER_SCRIPT, "sshd")
    assert command[10:16] == (
        "54321:54322",
        "LEASETOKEN",
        "90",
        f"/home/alice/.local/state/ezhpcy/{EZHPCY_VERSION}/logs/worker",
        "/home/alice/.cache/ezhpcy/control/test",
        "/opt/pbspro/bin/qdel",
    )
    worker_script = WORKER_SCRIPT
    assert "command -v sshd" in worker_script
    assert 'ports="$1"' in worker_script
    assert "shift 6" in worker_script
    assert 'port="${ports%%:*}"' in worker_script
    assert '"$sshd_path" -D -e -p "$port" "$@"' in worker_script
    assert 'publish_state ready "$port"' in worker_script
    assert 'mv -f "$temporary" "$control_dir/status"' in worker_script
    assert command.count("bash") == 1
    assert command.count("-c") == 1
    assert "PidFile=none" in command
    assert "HostKey=/remote/ssh_host_ed25519_key" in command
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 WORKERKEY" in command


@pytest.mark.parametrize(
    ("interval", "timeout", "expected"),
    [
        (30, 90, (30, 3)),
        (20, 60, (20, 3)),
        (30, 100, (30, 3)),
        (0.5, 5, (1, 5)),
        (30, 20, (30, 1)),
    ],
)
def test_client_alive_gives_up_within_the_heartbeat_timeout(
    interval: float, timeout: int, expected: tuple[int, int]
) -> None:
    assert client_alive(interval, timeout) == expected


def test_select_ports_returns_unique_dynamic_ports() -> None:
    with patch("ezhpcy.worker.job.secrets.randbelow", side_effect=[0, 0, 1, 2, 3, 4]):
        ports = select_ports(5)

    assert ports == (49152, 49153, 49154, 49155, 49156)


# The lease watchdog, run by bash with a stand-in for sshd and for the scheduler's
# cancel command

requires_bash = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")


@pytest.fixture
def sshd() -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(["sleep", "60"])
    yield process
    process.kill()
    process.wait()


def run_watchdog(
    tmp_path: Path, sshd_pid: int, *, cancel_status: int = 0
) -> tuple[str, str]:
    """
    Run the watchdog to its end, with a heartbeat timeout of a second and one
    lease; return what the cancel command was called with, and the heartbeat log.
    """
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    (control_dir / "lease.00000000000000000001").write_text("LEASETOKEN 1\n")
    cancel = tmp_path / "cancel"
    cancel.write_text(
        f'#!/bin/sh\necho "$@" >> "{tmp_path}/cancelled"\n'
        f"echo 'Job <42> is being terminated'\nexit {cancel_status}\n"
    )
    cancel.chmod(0o755)
    log = tmp_path / "heartbeat.log"
    subprocess.run(
        ["bash", "-c", WATCHDOG_SCRIPT, "watchdog", "LEASETOKEN", "1", str(log)]
        + [str(control_dir), "42", str(cancel), str(sshd_pid)],
        check=True,
        timeout=30,
    )
    return (tmp_path / "cancelled").read_text(), log.read_text()


@requires_bash
def test_watchdog_cancels_the_job_once_sshd_has_exited(
    tmp_path: Path, sshd: subprocess.Popen[bytes]
) -> None:
    # As when the scheduler has killed the job's terminal session.
    sshd.kill()
    sshd.wait()

    cancelled, log = run_watchdog(tmp_path, sshd.pid)

    assert cancelled == "42\n"
    assert (
        "event=job_cancelled job=42 reason=sshd_exited "
        "output=Job <42> is being terminated\n"
    ) in log


@requires_bash
def test_watchdog_cancels_the_job_once_the_leases_stop(
    tmp_path: Path, sshd: subprocess.Popen[bytes]
) -> None:
    cancelled, log = run_watchdog(tmp_path, sshd.pid)

    assert cancelled == "42\n"
    assert "event=heartbeat_timeout job=42 last_sequence=1" in log
    assert "event=job_cancelled job=42 reason=heartbeat_timeout" in log
    # It leaves stopping sshd to the scheduler.
    assert sshd.poll() is None


@requires_bash
def test_watchdog_stops_sshd_when_it_cannot_cancel_the_job(
    tmp_path: Path, sshd: subprocess.Popen[bytes]
) -> None:
    cancelled, log = run_watchdog(tmp_path, sshd.pid, cancel_status=1)

    assert cancelled == "42\n"
    assert (
        "event=job_cancel_failed job=42 reason=heartbeat_timeout exit_status=1"
    ) in log
    assert sshd.wait(timeout=5) == -15


# WorkerControl, over SFTP to the in-process server


@asynccontextmanager
async def control(
    server: SSHTestServer, ports: tuple[int, ...] = (54321,)
) -> AsyncGenerator[WorkerControl]:
    conn_info = Login(user=server.user, host=server.host)
    async with (
        await ssh.connect(
            conn_info,
            port=server.port,
            client_keys=[server.client_key],
            agent_path=None,
            known_hosts=server.known_hosts,
        ) as host,
        host.sftp() as sftp,
    ):
        yield WorkerControl(
            sftp, _CONTROL_DIR, "LEASETOKEN", ports=ports, app_ports=(60001,)
        )


def write_file(server: SSHTestServer, name: str, content: str) -> None:
    server.local_path(_CONTROL_DIR / name).write_text(content, encoding="utf-8")


def write_status(server: SSHTestServer, record: str) -> None:
    """As the worker does, in Latin-1, so a test can write bytes that aren't UTF-8."""
    server.local_path(_CONTROL_DIR / "status").write_bytes(record.encode("latin-1"))


def leases(server: SSHTestServer) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in server.local_path(_CONTROL_DIR).iterdir()
    }


async def test_worker_control_keeps_the_latest_two_leases(
    ssh_server: SSHTestServer,
) -> None:
    async with control(ssh_server) as worker_control:
        await worker_control.create()
        sequences = [await worker_control.publish_lease() for _ in range(4)]
        published = await asyncio.to_thread(leases, ssh_server)

    assert sequences == [1, 2, 3, 4]
    # No temporary file is left behind.
    assert published == {
        "lease.00000000000000000003": "LEASETOKEN 3\n",
        "lease.00000000000000000004": "LEASETOKEN 4\n",
    }


async def test_worker_control_reads_no_status_until_the_worker_writes_one(
    ssh_server: SSHTestServer,
) -> None:
    async with control(ssh_server) as worker_control:
        await worker_control.create()

        assert await worker_control.read_status() is None


@pytest.mark.parametrize(
    ("record", "status"),
    [
        ("LEASETOKEN ready 54322\n", WorkerReady(54322)),
        ("LEASETOKEN failed no_ports\n", WorkerFailed("no_ports")),
        ("LEASETOKEN failed\n", WorkerFailed("worker startup failed")),
        ("LEASETOKEN stopped sshd 75\n", WorkerStopped("sshd", 75)),
        ("LEASETOKEN stopped app 3\n", WorkerStopped("app", 3)),
        ("LEASETOKEN ready 54322 socket\n", WorkerReady(54322, "socket")),
        ("LEASETOKEN ready 54322 60001\n", WorkerReady(54322, 60001)),
    ],
)
async def test_worker_control_reads_the_status(
    ssh_server: SSHTestServer, record: str, status: WorkerStatus
) -> None:
    async with control(ssh_server, ports=(54321, 54322)) as worker_control:
        await worker_control.create()
        await asyncio.to_thread(write_status, ssh_server, record)

        assert await worker_control.read_status() == status


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ("LEASETOKEN ready 54322", "unexpected SSH port 54322"),
        ("LEASETOKEN ready port", "non-numeric SSH port"),
        ("LEASETOKEN ready", "invalid status record"),
        ("LEASETOKEN stopped", "invalid status record"),
        ("LEASETOKEN stopped -", "invalid status record"),
        # Without which process stopped, as before apps.
        ("LEASETOKEN stopped 75", "invalid status record"),
        ("LEASETOKEN stopped sshd -", "invalid status record"),
        ("LEASETOKEN stopped kernel 1", "invalid status record"),
        ("LEASETOKEN ready 54321 1234", "unexpected app port 1234"),
        ("LEASETOKEN ready 54321 sock", "non-numeric app port"),
        ("LEASETOKEN paused", "invalid status record"),
        ("OTHERTOKEN ready 54321", "invalid status record"),
        # Bytes that aren't UTF-8.
        ("LEASETOKEN\xff ready 54321", "invalid status record"),
    ],
)
async def test_worker_control_rejects_an_unusable_status(
    ssh_server: SSHTestServer, record: str, message: str
) -> None:
    async with control(ssh_server) as worker_control:
        await worker_control.create()
        await asyncio.to_thread(write_status, ssh_server, record)
        with pytest.raises(WorkerError, match=message):
            await worker_control.read_status()


async def test_worker_control_close_removes_the_directory(
    ssh_server: SSHTestServer,
) -> None:
    async with control(ssh_server) as worker_control:
        await worker_control.create()
        await worker_control.publish_lease()
        await asyncio.to_thread(write_status, ssh_server, "LEASETOKEN stopped sshd 0\n")
        # Left by heartbeats cancelled mid-write: a temporary lease, and a lease
        # renamed but not yet tracked.
        for name in (".lease.00000000000000000002.tmp", "lease.00000000000000000002"):
            await asyncio.to_thread(write_file, ssh_server, name, "LEASETOKEN 2\n")
        await worker_control.close()

    assert not ssh_server.local_path(_CONTROL_DIR).exists()
    # The root stays, for the next tunnel.
    assert ssh_server.local_path(_CONTROL_DIR.parent).is_dir()
