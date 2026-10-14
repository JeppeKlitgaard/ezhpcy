"""Tests for `ezhpcy.cli.utils.ssh.connect_interactive`, against the in-process SSH server."""

import asyncio
import logging
import signal
import socket
import time
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import asyncssh
import pytest
from rich.prompt import Confirm, Prompt

from ezhpcy.cli.utils.ssh import connect_interactive
from ezhpcy.ssh import RemoteHost
from ezhpcy.types import ConnectionInfo
from tests.support.sshserver import KbdintPrompt, SSHTestServer, reply

WAIT_SECONDS = 5


class Prompts:
    """Answers the CLI's prompts from a list, and records what was asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A string or bool to answer with, or an exception to raise at the prompt.
        self.answers: list[object] = []
        # (prompt, whether input is hidden)
        self.asked: list[tuple[str, bool]] = []
        for owner in (Prompt, Confirm):
            monkeypatch.setattr(owner, "ask", self._ask)

    def _ask(
        self, prompt: object, *args: object, password: bool = False, **_
    ) -> object:
        self.asked.append((str(prompt), password))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> Prompts:
    return Prompts(monkeypatch)


@pytest.fixture
def system_known_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """~/.ssh/known_hosts, in a home directory of the test's own."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home / ".ssh" / "known_hosts"


@pytest.fixture
def known_hosts_file(tmp_path: Path, system_known_hosts: Path) -> Path:
    """EzHPCy's own known hosts file."""
    return tmp_path / "ezhpcy" / "known_hosts"


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    """Warnings logged by `ezhpcy.cli.utils.ssh` (the `ezhpcy` logger doesn't
    propagate, so `caplog` doesn't see them)."""
    messages: list[str] = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = lambda record: messages.append(record.getMessage())
    logger = logging.getLogger("ezhpcy.cli.utils.ssh")
    logger.addHandler(handler)
    yield messages
    logger.removeHandler(handler)


def _connect(
    server: SSHTestServer,
    known_hosts_file: Path,
    *,
    host: str | None = None,
    password: str | None = None,
    password_prompt: bool = True,
    **options: object,
) -> AbstractAsyncContextManager[RemoteHost]:
    # `model_construct`, because `ConnectionInfo.host` accepts domain names only.
    conn_info = ConnectionInfo.model_construct(
        user=server.user,
        host=host or server.host,
        password=password,
        password_prompt=password_prompt,
    )
    defaults: dict[str, object] = {
        "port": server.port,
        "client_keys": [server.client_key],
        "agent_path": None,
    }
    return connect_interactive(
        conn_info, known_hosts_file=known_hosts_file, **(defaults | options)
    )


def _trust(server: SSHTestServer, file: Path) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(server.known_hosts_line)


def _other_key_line(pattern: str) -> str:
    key = asyncssh.generate_private_key("ssh-ed25519")
    key_type, key_data, *_ = key.export_public_key("openssh").decode().split()
    return f"{pattern} {key_type} {key_data}\n"


def _only_passwords(server: SSHTestServer, password: str = "secret") -> None:
    """The server rejects the client's key and accepts `password`."""
    server.auth.client_keys = [asyncssh.generate_private_key("ssh-ed25519")]
    server.auth.password = password


def _password_attempts(server: SSHTestServer) -> list[bool]:
    return [
        accepted
        for method, accepted in server.auth_attempts
        if method in {"password", "keyboard-interactive"}
    ]


# Authentication


async def test_key_authentication_needs_no_prompt(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    _trust(ssh_server, known_hosts_file)
    ssh_server.command("hostname", reply(stdout="login1\n"))

    async with _connect(ssh_server, known_hosts_file) as host:
        assert await host.run(["hostname"]) == "login1\n"

    assert prompts.asked == []
    assert ssh_server.auth_attempts == [("publickey", True)]


async def test_password_is_asked_for_after_keys_fail(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)
    prompts.answers = ["secret"]

    async with _connect(ssh_server, known_hosts_file):
        pass

    assert prompts.asked == [("Enter password for alice@127.0.0.1", True)]
    # The password is given to a second connection, which tries the key again.
    assert ssh_server.auth_attempts == [
        ("publickey", False),
        ("publickey", False),
        ("password", True),
    ]


async def test_a_wrong_password_can_be_retried(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    prompts: Prompts,
    capsys: pytest.CaptureFixture[str],
):
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)
    prompts.answers = ["wrong", "secret"]

    async with _connect(ssh_server, known_hosts_file):
        pass

    assert _password_attempts(ssh_server) == [False, True]
    assert "Error [1/3]: Invalid password. Try again or press Ctrl+C to abort." in (
        capsys.readouterr().out
    )


async def test_three_wrong_passwords_fail(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    prompts: Prompts,
    capsys: pytest.CaptureFixture[str],
):
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)
    prompts.answers = ["wrong", "wrong", "wrong"]

    with pytest.raises(asyncssh.PermissionDenied):
        async with _connect(ssh_server, known_hosts_file):
            pass

    assert len(prompts.asked) == 3
    assert _password_attempts(ssh_server) == [False, False, False]
    output = capsys.readouterr().out
    assert "Error [1/3]" in output
    assert "Error [2/3]" in output
    assert "[3/3]" not in output


async def test_a_prompted_password_answers_a_keyboard_interactive_password_prompt(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    _trust(ssh_server, known_hosts_file)
    ssh_server.auth.client_keys = []
    ssh_server.auth.kbdint = [KbdintPrompt("Password: ", "secret")]
    prompts.answers = ["secret"]

    async with _connect(ssh_server, known_hosts_file):
        pass

    assert prompts.asked == [("Enter password for alice@127.0.0.1", True)]
    assert _password_attempts(ssh_server) == [True]


async def test_each_password_is_tried_once_whatever_the_methods(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)
    ssh_server.auth.kbdint = [KbdintPrompt("Password: ", "secret")]
    prompts.answers = ["wrong", "wrong", "wrong"]

    with pytest.raises(asyncssh.PermissionDenied):
        async with _connect(ssh_server, known_hosts_file):
            pass

    assert len(prompts.asked) == 3
    assert _password_attempts(ssh_server) == [False, False, False]


@pytest.mark.parametrize(("password", "connects"), [("secret", True), ("wrong", False)])
async def test_a_profile_password_is_tried_once_without_a_prompt(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    prompts: Prompts,
    password: str,
    connects: bool,
):
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)

    try:
        async with _connect(ssh_server, known_hosts_file, password=password):
            pass
    except asyncssh.PermissionDenied:
        assert not connects
    else:
        assert connects

    assert prompts.asked == []
    assert _password_attempts(ssh_server) == [connects]


async def test_a_profile_password_answers_a_keyboard_interactive_password_prompt(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    _trust(ssh_server, known_hosts_file)
    ssh_server.auth.client_keys = []
    ssh_server.auth.kbdint = [KbdintPrompt("Password: ", "secret")]

    async with _connect(ssh_server, known_hosts_file, password="secret"):
        pass

    assert prompts.asked == []
    assert _password_attempts(ssh_server) == [True]


async def test_no_prompt_when_prompting_is_off(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)
    ssh_server.auth.kbdint = [KbdintPrompt("Password: ", "secret")]

    with pytest.raises(asyncssh.PermissionDenied):
        async with _connect(ssh_server, known_hosts_file, password_prompt=False):
            pass

    assert prompts.asked == []
    assert _password_attempts(ssh_server) == []


async def test_ctrl_c_at_the_password_prompt_stops_connecting(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)
    prompts.answers = [KeyboardInterrupt()]

    with pytest.raises(KeyboardInterrupt):
        async with _connect(ssh_server, known_hosts_file):
            pass

    assert len(prompts.asked) == 1
    assert _password_attempts(ssh_server) == []


def test_ctrl_c_at_the_password_prompt_stops_the_command_at_once(
    ssh_server: SSHTestServer, known_hosts_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """A real SIGINT while the prompt waits, under `asyncio.run` as Cyclopts runs
    async commands: one Ctrl+C is enough."""
    _trust(ssh_server, known_hosts_file)
    _only_passwords(ssh_server)

    def interrupted(*_args: object, **_kwargs: object) -> str:
        signal.raise_signal(signal.SIGINT)
        time.sleep(WAIT_SECONDS)
        return "too late"

    monkeypatch.setattr(Prompt, "ask", interrupted)

    async def main() -> None:
        async with _connect(ssh_server, known_hosts_file):
            pass

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(main())

    assert time.monotonic() - started < WAIT_SECONDS
    assert _password_attempts(ssh_server) == []


# Host keys

# ruff: disable[ASYNC240]  # Known hosts files are small and local to the test


async def test_an_accepted_host_key_is_saved_to_ezhpcys_file(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    system_known_hosts: Path,
    prompts: Prompts,
    capsys: pytest.CaptureFixture[str],
):
    prompts.answers = [True]

    async with _connect(ssh_server, known_hosts_file):
        pass

    output = capsys.readouterr().out
    assert "The host key for 127.0.0.1 is not in any known hosts file." in output
    assert "Key type: ssh-ed25519" in output
    assert f"Key fingerprint: {ssh_server.host_key.get_fingerprint()}" in output
    assert prompts.asked == [("Do you want to accept this host key?: ", False)]
    assert known_hosts_file.read_text() == ssh_server.known_hosts_line
    assert not system_known_hosts.exists()

    # Known from now on.
    async with _connect(ssh_server, known_hosts_file):
        pass
    assert len(prompts.asked) == 1


async def test_an_accepted_key_is_appended_on_a_line_of_its_own(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    other = _other_key_line("other.example.com").rstrip("\n")
    known_hosts_file.parent.mkdir(parents=True)
    known_hosts_file.write_text(other)
    prompts.answers = [True]

    async with _connect(ssh_server, known_hosts_file):
        pass

    assert known_hosts_file.read_text() == f"{other}\n{ssh_server.known_hosts_line}"


async def test_a_rejected_host_key_is_not_saved(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    prompts.answers = [False]

    with pytest.raises(asyncssh.HostKeyNotVerifiable, match="rejected by user"):
        async with _connect(ssh_server, known_hosts_file):
            pass

    assert not known_hosts_file.exists()
    assert ssh_server.auth_attempts == []


async def test_ctrl_c_at_the_host_key_prompt_stops_connecting(
    ssh_server: SSHTestServer, known_hosts_file: Path, prompts: Prompts
):
    prompts.answers = [KeyboardInterrupt()]

    with pytest.raises(KeyboardInterrupt):
        async with _connect(ssh_server, known_hosts_file):
            pass

    assert not known_hosts_file.exists()


async def test_a_host_key_that_cant_be_saved_is_trusted_for_this_connection(
    ssh_server: SSHTestServer,
    tmp_path: Path,
    system_known_hosts: Path,
    prompts: Prompts,
    warnings: list[str],
):
    # Its directory is a file.
    (tmp_path / "not-a-directory").write_text("")
    known_hosts_file = tmp_path / "not-a-directory" / "known_hosts"
    prompts.answers = [True, True]

    async with _connect(ssh_server, known_hosts_file):
        pass
    async with _connect(ssh_server, known_hosts_file):
        pass

    # Asked again, as the key wasn't remembered.
    assert len(prompts.asked) == 2
    assert len(warnings) == 2
    assert warnings[0].startswith(
        f"Could not save the host key for 127.0.0.1 to {known_hosts_file}: "
    )


@pytest.mark.parametrize("in_system_file", [True, False])
async def test_a_changed_host_key_is_refused_without_a_prompt(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    system_known_hosts: Path,
    prompts: Prompts,
    in_system_file: bool,
):
    file = system_known_hosts if in_system_file else known_hosts_file
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(_other_key_line(f"[{ssh_server.host}]:{ssh_server.port}"))

    with pytest.raises(asyncssh.HostKeyNotVerifiable) as raised:
        async with _connect(ssh_server, known_hosts_file):
            pass

    assert prompts.asked == []
    assert str(raised.value) == (
        f"The ssh-ed25519 host key of 127.0.0.1 "
        f"({ssh_server.host_key.get_fingerprint()}) does not match the key for it "
        f"in {file}. If the host's key was changed on purpose, remove the old key "
        "from that file."
    )


async def test_a_bad_line_in_the_system_file_is_skipped(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    system_known_hosts: Path,
    prompts: Prompts,
    warnings: list[str],
):
    system_known_hosts.write_text(
        f"@unknown-marker example.com ssh-ed25519 AAAA\n{ssh_server.known_hosts_line}"
    )

    async with _connect(ssh_server, known_hosts_file):
        pass

    assert prompts.asked == []
    assert len(warnings) == 1
    assert warnings[0].startswith(
        f"Skipping line 1 of known hosts file {system_known_hosts}: "
    )


async def test_the_system_file_is_never_written(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    system_known_hosts: Path,
    prompts: Prompts,
    warnings: list[str],
):
    ca_line = _other_key_line("@cert-authority *.example.com")
    original = f"# Managed by hand\n{ca_line}{_other_key_line('other.example.com')}"
    system_known_hosts.write_bytes(original.encode())
    prompts.answers = [True]

    async with _connect(ssh_server, known_hosts_file):
        pass

    assert system_known_hosts.read_bytes() == original.encode()
    assert known_hosts_file.read_text() == ssh_server.known_hosts_line
    # The @cert-authority line was read, not skipped.
    assert warnings == []


@pytest.mark.parametrize("known", [False, True])
async def test_an_unknown_host_is_offered_openssh_s_key_types(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    prompts: Prompts,
    monkeypatch: pytest.MonkeyPatch,
    known: bool,
):
    if known:
        _trust(ssh_server, known_hosts_file)
    prompts.answers = [True]
    offered: list[object] = []
    original_connect = asyncssh.connect

    async def recording_connect(host: str, **options: object):
        offered.append(options.get("server_host_key_algs"))
        return await original_connect(host, **options)

    monkeypatch.setattr(asyncssh, "connect", recording_connect)

    async with _connect(ssh_server, known_hosts_file):
        pass

    if known:
        # asyncssh's choice: the types of the known keys.
        assert offered == [None]
    else:
        # The first attempt is refused, to ask about the key; the second is
        # made knowing it.
        assert offered[1] is None
        # OpenSSH's default (`ssh -G`, OpenSSH 9.5) without certificate types.
        assert offered[0] == [
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
            "sk-ssh-ed25519@openssh.com",
            "sk-ecdsa-sha2-nistp256@openssh.com",
            "rsa-sha2-512",
            "rsa-sha2-256",
        ]


async def test_a_host_known_only_by_its_address_counts_as_unknown(
    ssh_server: SSHTestServer,
    known_hosts_file: Path,
    system_known_hosts: Path,
    prompts: Prompts,
):
    """As in OpenSSH, which no longer checks host keys by IP address by default."""
    # Another key for the server's address, none for the name it's reached by.
    system_known_hosts.write_text(
        _other_key_line(f"[{ssh_server.host}]:{ssh_server.port}")
    )
    prompts.answers = [True]

    # IPv4 only, so "localhost" is the server's address.
    async with _connect(
        ssh_server, known_hosts_file, host="localhost", family=socket.AF_INET
    ):
        pass

    # Asked about, not refused as a changed key.
    assert prompts.asked == [("Do you want to accept this host key?: ", False)]
    assert known_hosts_file.read_text().startswith(f"[localhost]:{ssh_server.port} ")


# ruff: enable[ASYNC240]
