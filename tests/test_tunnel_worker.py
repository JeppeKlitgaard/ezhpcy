"""The worker job's command, and its control directory over real SFTP."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from unittest.mock import patch

import pytest

from ezhpcy import ssh
from ezhpcy.constants import EZHPCY_VERSION, OPENSSH_MATCHSPEC, PIXI_VERSION
from ezhpcy.tunnel import TunnelError
from ezhpcy.tunnel.sshd import retrying_sshd_script, sshd_config_arguments
from ezhpcy.tunnel.worker import (
    WorkerControl,
    select_worker_ports,
    worker_client_alive,
    worker_job_script,
    worker_sshd_command,
)
from ezhpcy.types import ConnectionInfo, RemoteState
from tests.support.sshserver import SSHTestServer

_CONTROL_DIR = PurePosixPath("/home/alice/state/worker_control/LEASETOKEN")


def test_worker_sshd_command_retries_ports_through_pixi() -> None:
    remote_state = RemoteState(
        cache_dir=PurePosixPath("/home/alice/.cache"),
        state_dir=PurePosixPath("/home/alice/.local/state"),
    )
    command = worker_sshd_command(
        remote_state,
        ports=(54321, 54322),
        heartbeat_token="LEASETOKEN",
        heartbeat_timeout_seconds=89.5,
        heartbeat_debug=True,
        control_dir=PurePosixPath("/home/alice/.cache/ezhpcy/control/test"),
        read_script_from_stdin=False,
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
    assert command[6:10] == ("bash", "-c", retrying_sshd_script(), "sshd")
    assert command[10:16] == (
        "54321:54322",
        "LEASETOKEN",
        # Whole seconds, rounded up, for the watchdog's integer comparison.
        "90",
        "1",
        f"/home/alice/.local/state/ezhpcy/{EZHPCY_VERSION}/logs/worker",
        "/home/alice/.cache/ezhpcy/control/test",
    )
    worker_script = retrying_sshd_script()
    assert "command -v sshd" in worker_script
    assert 'ports="$1"' in worker_script
    assert "shift 6" in worker_script
    assert 'port="${ports%%:*}"' in worker_script
    assert '"$sshd_path" -D -e -p "$port" "$@"' in worker_script
    assert 'publish_state ready "$port"' in worker_script
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
def test_worker_client_alive_gives_up_within_the_heartbeat_timeout(
    interval: float, timeout: float, expected: tuple[int, int]
) -> None:
    assert worker_client_alive(interval, timeout) == expected


def test_worker_job_script_feeds_the_worker_script_to_the_command() -> None:
    script = worker_job_script(("bash", "-s", "--", "two words"), "echo worker\n")

    assert script == (
        "#!/usr/bin/env bash\n"
        "exec bash -s -- 'two words' <<'EZHPCY_WORKER_SCRIPT'\n"
        "echo worker\n"
        "EZHPCY_WORKER_SCRIPT\n"
    )


def test_worker_job_script_rejects_a_script_containing_its_delimiter() -> None:
    with pytest.raises(ValueError, match="delimiter"):
        worker_job_script(("bash",), "echo\nEZHPCY_WORKER_SCRIPT\necho\n")


def test_select_worker_ports_returns_unique_dynamic_ports() -> None:
    with patch(
        "ezhpcy.tunnel.worker.secrets.randbelow", side_effect=[0, 0, 1, 2, 3, 4]
    ):
        ports = select_worker_ports(5)

    assert ports == (49152, 49153, 49154, 49155, 49156)


# WorkerControl, over SFTP to the in-process server


@asynccontextmanager
async def control(
    server: SSHTestServer, ports: tuple[int, ...] = (54321,)
) -> AsyncGenerator[WorkerControl]:
    conn_info = ConnectionInfo.model_construct(user=server.user, host=server.host)
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
        yield WorkerControl(sftp, _CONTROL_DIR, "LEASETOKEN", ports=ports)


def write_record(server: SSHTestServer, name: str, content: str) -> None:
    """As the worker does."""
    server.local_path(_CONTROL_DIR / name).write_text(content, encoding="utf-8")


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


async def test_worker_control_reads_the_ready_port(ssh_server: SSHTestServer) -> None:
    async with control(ssh_server, ports=(54321, 54322)) as worker_control:
        await worker_control.create()
        before = await worker_control.read_ready_port()
        await asyncio.to_thread(
            write_record, ssh_server, "ready", "v1 LEASETOKEN ready 54322\n"
        )
        after = await worker_control.read_ready_port()

    assert (before, after) == (None, 54322)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ("v1 LEASETOKEN ready 54322", "unexpected SSH port 54322"),
        ("v1 LEASETOKEN ready port", "non-numeric SSH port"),
        ("v1 OTHERTOKEN ready 54321", "invalid ready record"),
        ("v1 LEASETOKEN ready", "invalid ready record"),
    ],
)
async def test_worker_control_rejects_an_unusable_ready_record(
    ssh_server: SSHTestServer, record: str, message: str
) -> None:
    async with control(ssh_server) as worker_control:
        await worker_control.create()
        await asyncio.to_thread(write_record, ssh_server, "ready", record)
        with pytest.raises(TunnelError, match=message):
            await worker_control.read_ready_port()


@pytest.mark.parametrize(
    ("record", "failure"),
    [
        (None, None),
        ("v1 LEASETOKEN failed no_ports\n", "no_ports"),
        ("v1 LEASETOKEN failed\n", "worker startup failed"),
        ("v1 OTHERTOKEN failed no_ports\n", "worker wrote an invalid failure record"),
    ],
)
async def test_worker_control_reads_the_failure(
    ssh_server: SSHTestServer, record: str | None, failure: str | None
) -> None:
    async with control(ssh_server) as worker_control:
        await worker_control.create()
        if record is not None:
            await asyncio.to_thread(write_record, ssh_server, "failed", record)

        assert await worker_control.read_failure() == failure


async def test_worker_control_close_removes_the_directory(
    ssh_server: SSHTestServer,
) -> None:
    async with control(ssh_server) as worker_control:
        await worker_control.create()
        await worker_control.publish_lease()
        for name in ("ready", "stopped"):
            await asyncio.to_thread(
                write_record, ssh_server, name, f"v1 LEASETOKEN {name} 0\n"
            )
        await worker_control.close()

    assert not ssh_server.local_path(_CONTROL_DIR).exists()
    # The root stays, for the next tunnel.
    assert ssh_server.local_path(_CONTROL_DIR.parent).is_dir()
