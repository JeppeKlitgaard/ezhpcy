"""
A second in-process SSH server as the worker's sshd, reached through the login
server as the real one is: `connect_worker` opens its connection over a
`direct-tcpip` channel of the login connection. Use the `worker` fixture.

`FakeProcesses` plays what `ezhpcy.process` runs on a host, as the test server
runs no real commands: the detached start, and the signals that stop it.
"""

import asyncio
import shlex
import shutil
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import asyncssh
import pytest

from ezhpcy import process
from ezhpcy.constants import WORKER_HOST_ALIAS
from ezhpcy.provision_local import WorkerCredentials
from tests.support.login import MACHINE_ID
from tests.support.sshserver import Auth, Invocation, SSHTestServer, reply
from tests.support.tunnel import write_atomically

# The signals `kill` sends, by name.
_SIGNAL_NUMBERS = {"HUP": 1, "INT": 2, "KILL": 9, "TERM": 15}
# What the worker's `hostname` says.
NODE = "node-1"


def worker_credentials() -> WorkerCredentials:
    """The worker's credentials, created and pinned as `ezhpcy provision` does."""
    credentials = WorkerCredentials(MACHINE_ID, SSHTestServer.user, "login.example.com")
    credentials.ensure_local_keys()
    credentials.pin_host_key()
    return credentials


def remove_worker_credentials() -> None:
    """Remove the worker's local credentials, as if never provisioned."""
    shutil.rmtree(worker_credentials().local_dir)


@contextmanager
def worker_sshd(
    login: SSHTestServer, root: Path, credentials: WorkerCredentials
) -> Generator[SSHTestServer]:
    """
    Serve the worker's sshd with `credentials`' host key, accepting their client
    key, on a port `login` may forward to. Its `hostname` says `NODE`.
    """
    worker = SSHTestServer(
        root, host_key=asyncssh.read_private_key(credentials.host_key)
    )
    worker.auth = Auth(client_keys=[asyncssh.read_private_key(credentials.client_key)])
    worker.command("hostname", reply(stdout=f"{NODE}\n"))
    with worker:
        login.allow_forwarding(worker.port)
        yield worker


def ssh_through(port: int, command: str = "hostname") -> str:
    """
    As `ssh <alias>` does through a tunnel's Host block: connect to the loopback
    `port` with the worker's credentials, checking its pinned host key, and
    return what `command` prints. Blocks, so call it from a thread.
    """
    credentials = worker_credentials()

    async def run() -> str:
        async with asyncssh.connect(
            "127.0.0.1",
            port,
            username=credentials.login_user,
            client_keys=[str(credentials.client_key)],
            known_hosts=str(credentials.known_hosts),
            host_key_alias=WORKER_HOST_ALIAS,
            agent_path=None,
            config=None,
        ) as conn:
            return str((await conn.run(command, check=True)).stdout)

    return asyncio.run(run())


def quick_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """See processes' exits within 10 ms, and kill them 0.2 s after TERM."""
    monkeypatch.setattr(process, "_STATUS_POLL_SECONDS", 0.01)
    monkeypatch.setattr(process, "_STOP_GRACE_SECONDS", 0.2)


@dataclass
class FakeProcess:
    """A process `FakeProcesses` started: its run directory, and its group."""

    run_dir: Path
    pgid: int
    # The signals sent to its group, in order.
    signals: list[str] = field(default_factory=list)

    @property
    def launch_script(self) -> str:
        return (self.run_dir / "launch.sh").read_text(encoding="utf-8")

    def exit(self, status: int) -> None:
        """Exit with `status`, as the launch script reports it."""
        write_atomically(self.run_dir / "status", f"{status}\n")

    def write_stderr(self, text: str) -> None:
        with (self.run_dir / "err").open("a", encoding="utf-8") as stderr:
            stderr.write(text)


class FakeProcesses:
    """
    Plays processes started on `server`: `setsid -f bash <run dir>/launch.sh`
    says the next process group, as the launch script does, and the stop
    script (`ezhpcy.process.STOP_SCRIPT`) creates `stop`, records the signal
    and, unless the process ignores it (`ignore`), makes it exit as bash
    reports a death by that signal; KILL kills the launch script too, so no
    status is written. `started` is set at each start, and `on_signal` is
    called at each signal, on the server's thread.
    """

    def __init__(
        self,
        server: SSHTestServer,
        *,
        on_signal: Callable[[FakeProcess, str], object] = lambda _process, _name: None,
    ) -> None:
        self.server = server
        self.processes: list[FakeProcess] = []
        self.ignore: set[str] = set()
        self.started = threading.Event()
        self._on_signal = on_signal
        self._next_pgid = 4000
        # Where run directories go.
        server.local_path("/tmp").mkdir(parents=True, exist_ok=True)
        server.command("setsid -f bash", self._start)
        server.command(shlex.join(["bash", "-c", process.STOP_SCRIPT]), self._kill)
        server.command("tail", self._tail)
        server.command("rm -rf --", self._remove)

    def _start(self, invocation: Invocation) -> None:
        [launch] = invocation.argv[3:]
        self._next_pgid += 1
        process = FakeProcess(
            self.server.local_path(PurePosixPath(launch).parent), self._next_pgid
        )
        (process.run_dir / "out").touch()
        (process.run_dir / "err").touch()
        self.processes.append(process)
        invocation.stdout.write(f"{process.pgid}\n")
        self.started.set()

    def _kill(self, invocation: Invocation) -> int:
        run_dir, name, group = invocation.argv[4:]
        pgid = int(group)
        [process] = [process for process in self.processes if process.pgid == pgid]
        assert process.run_dir == self.server.local_path(run_dir)
        (process.run_dir / "stop").touch()
        process.signals.append(name)
        self._on_signal(process, name)
        if (process.run_dir / "status").exists():
            invocation.stderr.write(f"kill: (-{group}) - No such process\n")
            return 1
        if name != "KILL" and name not in self.ignore:
            process.exit(128 + _SIGNAL_NUMBERS[name])
        return 0

    def _tail(self, invocation: Invocation) -> int:
        _tail, _n, count, _separator, path = invocation.argv
        local = self.server.local_path(path)
        if not local.exists():
            invocation.stderr.write(f"tail: cannot open '{path}'\n")
            return 1
        lines = local.read_text(encoding="utf-8").splitlines(keepends=True)
        invocation.stdout.write("".join(lines[-int(count) :]))
        return 0

    def _remove(self, invocation: Invocation) -> None:
        for path in invocation.argv[3:]:
            local = self.server.local_path(path)
            for child in sorted(local.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            if local.exists():
                local.rmdir()
