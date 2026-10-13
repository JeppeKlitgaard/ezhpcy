"""Smoke tests for the in-process SSH server in `tests/support/sshserver.py`."""

import asyncio

import asyncssh
import pytest

from ezhpcy.cli import app
from tests.support.cli import invoke
from tests.support.sshserver import (
    Invocation,
    KbdintPrompt,
    SSHTestServer,
    reply,
)

LARGE_OUTPUT_BYTES = 5_000_000


async def test_public_key_authentication_and_a_canned_command(
    ssh_server: SSHTestServer,
):
    ssh_server.command("bjobs -noheader", reply(stdout="RUN\n", stderr="note\n"))

    async with await ssh_server.connect() as conn:
        result = await conn.run("bjobs -noheader 42", check=False)

    assert (result.exit_status, result.stdout, result.stderr) == (0, "RUN\n", "note\n")
    assert ssh_server.auth_attempts == [("publickey", True)]
    assert ssh_server.commands_run == [["bjobs", "-noheader", "42"]]


async def test_unknown_host_key_is_rejected(ssh_server: SSHTestServer):
    with pytest.raises(asyncssh.HostKeyNotVerifiable):
        await ssh_server.connect(known_hosts=asyncssh.SSHKnownHosts())


async def test_password_authentication_retries_within_one_connection(
    ssh_server: SSHTestServer,
):
    ssh_server.auth.client_keys = []
    ssh_server.auth.password = "secret"
    passwords = iter(["wrong", "secret"])

    class Client(asyncssh.SSHClient):
        def password_auth_requested(self) -> str:
            return next(passwords)

    async with await ssh_server.connect(client_factory=Client, client_keys=None):
        pass

    assert ssh_server.auth_attempts == [("password", False), ("password", True)]


async def test_authentication_fails_when_no_method_succeeds(ssh_server: SSHTestServer):
    ssh_server.auth.client_keys = []

    with pytest.raises(asyncssh.PermissionDenied):
        await ssh_server.connect()


async def test_keyboard_interactive_with_several_prompts(ssh_server: SSHTestServer):
    ssh_server.auth.client_keys = []
    ssh_server.auth.kbdint = [
        KbdintPrompt("Password: ", "secret"),
        KbdintPrompt("Verification code: ", "123456", echo=True),
    ]
    challenges: list[list[tuple[str, bool]]] = []

    class Client(asyncssh.SSHClient):
        def kbdint_auth_requested(self) -> str:
            return ""

        def kbdint_challenge_received(
            self,
            name: str,
            instructions: str,
            lang: str,
            prompts: list[tuple[str, bool]],
        ) -> list[str]:
            challenges.append(prompts)
            return ["secret", "123456"]

    async with await ssh_server.connect(client_factory=Client, client_keys=None):
        pass

    assert challenges == [[("Password: ", False), ("Verification code: ", True)]]
    assert ssh_server.auth_attempts == [("keyboard-interactive", True)]


async def test_unregistered_command_fails_like_a_shell(ssh_server: SSHTestServer):
    async with await ssh_server.connect() as conn:
        result = await conn.run("qstat -f", check=False)

    assert result.exit_status == 127
    assert result.stderr == "bash: qstat: command not found\n"


async def test_longest_matching_prefix_wins(ssh_server: SSHTestServer):
    ssh_server.command("", reply(stdout="anything\n"))
    ssh_server.command("bash -lc", reply(stdout="login shell\n"))
    ssh_server.command("bash -lc 'echo $HOME'", reply(stdout="/home/alice\n"))

    async with await ssh_server.connect() as conn:
        outputs = [
            (await conn.run(command, check=True)).stdout
            for command in ("bash -lc 'echo $HOME'", "bash -lc true", "true")
        ]

    assert outputs == ["/home/alice\n", "login shell\n", "anything\n"]


async def test_fake_reads_stdin_and_sets_the_exit_status(ssh_server: SSHTestServer):
    async def cat_then_fail(invocation: Invocation) -> int:
        invocation.stdout.write(await invocation.stdin.read())
        return 3

    ssh_server.command("cat", cat_then_fail)

    async with await ssh_server.connect() as conn:
        result = await conn.run("cat", input="hello\n", check=False)

    assert (result.exit_status, result.stdout) == (3, "hello\n")


async def test_large_stderr_does_not_block_the_command(ssh_server: SSHTestServer):
    async def noisy(invocation: Invocation) -> None:
        invocation.stderr.write("\0" * LARGE_OUTPUT_BYTES)
        await invocation.stderr.drain()
        invocation.stdout.write("ok\n")

    ssh_server.command("noisy", noisy)

    async with await ssh_server.connect() as conn:
        result = await asyncio.wait_for(conn.run("noisy", check=True), 30)

    assert result.stdout == "ok\n"
    assert len(result.stderr or "") == LARGE_OUTPUT_BYTES


async def test_pty_merges_stderr_into_stdout(ssh_server: SSHTestServer):
    async def interactive(invocation: Invocation) -> None:
        invocation.stdout.write(f"term={invocation.term_type}\n")
        invocation.stderr.write("Job <42> is submitted\n")
        line = await invocation.stdin.readline()
        invocation.stdout.write(f"got {line}")

    ssh_server.command("bsub -Is", interactive)

    async with await ssh_server.connect() as conn:
        process = await conn.create_process(
            "bsub -Is bash", term_type="xterm", encoding=None
        )
        process.stdin.write(b"hi\n")
        stdout = await process.stdout.read()
        completed = await process.wait(check=False)

    assert completed.exit_status == 0
    assert stdout == b"term=xterm\nJob <42> is submitted\ngot hi\n"


async def test_sftp_is_rooted_in_a_local_directory(ssh_server: SSHTestServer):
    async with (
        await ssh_server.connect() as conn,
        conn.start_sftp_client() as sftp,
    ):
        assert await sftp.realpath(".") == "/home/alice"
        await sftp.makedirs(".cache/ezhpcy/state", exist_ok=True)
        await sftp.chmod(".cache/ezhpcy", 0o700)
        async with sftp.open(".cache/ezhpcy/state/ready.tmp", "w") as file:
            await file.write('{"state": "ready"}\n')
        await sftp.posix_rename(
            ".cache/ezhpcy/state/ready.tmp", "/home/alice/.cache/ezhpcy/state/ready"
        )
        names = sorted(name.filename for name in await sftp.readdir(".cache/ezhpcy"))
        async with sftp.open(".cache/ezhpcy/state/ready") as file:
            text = await file.read()
        local = ssh_server.local_path("/home/alice/.cache/ezhpcy/state/ready")
        local_text = local.read_text(encoding="utf-8")
        await sftp.remove(".cache/ezhpcy/state/ready")
        await sftp.rmdir(".cache/ezhpcy/state")

    # Like OpenSSH's sftp-server, asyncssh lists `.` and `..`.
    assert names == [".", "..", "state"]
    assert text == local_text == '{"state": "ready"}\n'
    assert local == ssh_server.local_path(".cache/ezhpcy/state/ready")
    assert not local.parent.exists()


async def test_sftp_cannot_leave_its_root(ssh_server: SSHTestServer):
    async with (
        await ssh_server.connect() as conn,
        conn.start_sftp_client() as sftp,
    ):
        assert await sftp.realpath("../../../..") == "/"


async def test_forwarding_only_to_allowed_ports(ssh_server: SSHTestServer):
    async def banner(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"SSH-2.0-worker\r\n")
        await writer.drain()
        writer.close()

    worker = await asyncio.start_server(banner, "127.0.0.1", 0)
    port = worker.sockets[0].getsockname()[1]
    try:
        async with await ssh_server.connect() as conn:
            with pytest.raises(asyncssh.ChannelOpenError) as error:
                await conn.open_connection("127.0.0.1", port)
            assert error.value.code == asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED

            ssh_server.allow_forwarding(port)
            reader, writer = await conn.open_connection("127.0.0.1", port)
            assert await reader.readline() == b"SSH-2.0-worker\r\n"
            writer.close()
    finally:
        worker.close()
        await worker.wait_closed()


class _LossRecorder(asyncssh.SSHClient):
    def __init__(self) -> None:
        self.lost: asyncio.Future[Exception | None] = (
            asyncio.get_running_loop().create_future()
        )

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.lost.done():
            self.lost.set_result(exc)


async def test_blackhole_is_detected_by_keepalive(ssh_server: SSHTestServer):
    client = _LossRecorder()
    conn = await ssh_server.connect(
        client_factory=lambda: client, keepalive_interval=0.1, keepalive_count_max=2
    )
    # Keepalives are answered while the network works.
    await asyncio.sleep(0.5)
    assert not client.lost.done()

    ssh_server.blackhole()

    error = await asyncio.wait_for(client.lost, 5)
    assert isinstance(error, asyncssh.ConnectionLost)
    assert "keepalive" in str(error)
    conn.close()


async def test_drop_closes_the_connection(ssh_server: SSHTestServer):
    client = _LossRecorder()
    conn = await ssh_server.connect(client_factory=lambda: client)

    ssh_server.drop()

    error = await asyncio.wait_for(client.lost, 5)
    assert isinstance(error, asyncssh.ConnectionLost)
    conn.close()


async def test_deliberate_close_reports_no_loss(ssh_server: SSHTestServer):
    client = _LossRecorder()
    conn = await ssh_server.connect(client_factory=lambda: client)

    conn.close()

    assert await asyncio.wait_for(client.lost, 5) is None


def test_a_failing_fake_fails_the_test(tmp_path):
    def broken(_invocation: Invocation) -> None:
        raise ValueError("bug in the fake")

    async def run_broken(server: SSHTestServer) -> asyncssh.SSHCompletedProcess:
        async with await server.connect() as conn:
            return await conn.run("broken", check=False)

    server = SSHTestServer(tmp_path)
    server.command("broken", broken)
    server.start()
    result = asyncio.run(run_broken(server))

    # The client sees an ordinary failure...
    assert result.exit_status == 1
    assert "bug in the fake" in str(result.stderr)
    # ...and the test fails when the server stops.
    with pytest.raises(ValueError, match="bug in the fake"):
        server.stop()


def test_async_commands_run_through_invoke(ssh_server: SSHTestServer):
    """
    A synchronous test can `invoke` an `async def` command, which Cyclopts runs on
    a fresh event loop, while the server keeps running on its own.
    """
    ssh_server.command("hostname", reply(stdout="login1\n"))

    async def probe_cmd() -> None:
        async with await ssh_server.connect() as conn:
            print((await conn.run("hostname", check=True)).stdout, end="")

    app.command(probe_cmd, name="probe-async")
    try:
        result = invoke(["probe-async"])
    finally:
        del app["probe-async"]

    assert (result.exit_code, result.stdout, result.exception) == (0, "login1\n", None)
    assert invoke(["probe-async"]).exit_code == 2
