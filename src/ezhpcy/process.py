"""
Processes started detached on a host: they outlive the connection that started
them, and their exit status can be read later (decision 41 of PLAN.md).

`start` creates a private run directory on the host, `/tmp/ezhpcy-<run ID>/`,
over SFTP (mode 0700, failing if the path exists), writes the launch script into
it, and runs `setsid -f bash <run dir>/launch.sh`. The script, leading a session
and process group of its own:

1. traps HUP, INT and TERM with `true`, so that it outlives a signal to its
   group, and prints its PID, which is the group's;
2. gives up the session's stdin, stdout and stderr, so that the session ends
   (`out` and `err` in the run directory take the output);
3. runs the command in the foreground, then writes the status bash reports for
   it (`128+n` for a death by signal n) to `status`, by temporary file and `mv`.

The run directory also holds the process's record, `process.json`, so that its
processes can be found again on the host (`list_runs`). One `ProcessWatcher`
per host polls the status files of its processes, over one SFTP session.

Stopping signals the process group from another session: TERM, then KILL after
a grace period; then the run directory is removed. A signal may come after the
script printed its PID but before it started the command, which then misses it
(bash runs the trap only once the command it waits for has exited). So stopping
first creates `stop` in the run directory, and the command's subshell, once
started, exits with 143 instead if it is there, as if TERM had killed it.

Nothing secret goes on a command line: the command and its environment are in
the launch script, and only the run directory's path is on `setsid`'s, once the
directory exists and is the user's alone.

A process is attached to its host (`RemoteHost.attachments`): it is stopped when
the host's connection closes, unless it was stopped before. On a job's node,
the job's end ends it too. Elsewhere, nothing stops it if the client dies.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
lazy import asyncio
lazy import functools
lazy import json
lazy import secrets
lazy import shlex
lazy from collections.abc import Mapping, Sequence
lazy from dataclasses import replace
lazy from datetime import UTC, datetime
lazy from typing import Any

lazy import asyncssh

from ezhpcy.logging import get_logger
lazy from ezhpcy.attachments import Attachment
lazy from ezhpcy.cleanup import finish
lazy from ezhpcy.ssh import CommandFailed, RemoteHost

logger = get_logger(__name__)

# Node-local, and short enough for Unix sockets inside it (108 bytes).
_RUN_ROOT = PurePosixPath("/tmp")
_RUN_PREFIX = "ezhpcy-"
_LAUNCH_NAME = "launch.sh"
_RECORD_NAME = "process.json"
_STATUS_NAME = "status"
_STDERR_NAME = "err"
_STOP_NAME = "stop"
# How often to look for the status files: an exit is seen within this long.
_STATUS_POLL_SECONDS = 1.0
# How long a process may take to exit after TERM, before it is killed.
_STOP_GRACE_SECONDS = 5.0
# As bash reports a death by TERM (128 + 15), and by KILL (128 + 9).
_TERMINATED_STATUS = 143
_KILLED_STATUS = 137
# A name `export` takes.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Signals a process group from a session of its own: `bash -c STOP_SCRIPT stop
# <run dir> <signal> <pgid>`. `stop` comes first: see the module.
STOP_SCRIPT = f'touch -- "$1/{_STOP_NAME}"; kill -s "$2" -- "-$3"'


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    """A process started on a host (`Process`)."""

    run_id: str
    # Its command's first word, as given.
    executable: str
    # Its exit status, once it has exited: `128+n` for a death by signal n.
    exit_status: int | None = None


class Process:
    """
    A command started detached on a host, from `Host.start`, in a run directory
    of its own there (`run_dir`, named after `run_id`), which holds its output,
    `out` and `err`. Its exit ends only this handle, never its host or job.
    """

    def __init__(
        self,
        run_dir: PurePosixPath,
        pgid: int,
        exited: asyncio.Future[int],
        host: RemoteHost,
        attachment: Attachment,
    ) -> None:
        # Use `Host.start`.
        self.run_dir = run_dir
        self.run_id = run_dir.name.removeprefix(_RUN_PREFIX)
        # Its process group's ID on the host, which is the launch script's PID.
        self.pgid = pgid
        self._exited = exited
        self._host = host
        self._attachment = attachment

    @property
    def exit_status(self) -> int | None:
        """Its exit status once it has exited, else None (see `wait`)."""
        exited = self._exited
        if not exited.done() or exited.cancelled() or exited.exception() is not None:
            return None
        return exited.result()

    async def wait(self) -> int:
        """
        Wait until the process exits, and return its exit status: `128+n` for a
        death by signal n (137 when `stop` had to kill it). An exit is seen
        within about a second. Raises if the host's connection is lost first.
        """
        return await asyncio.shield(self._exited)

    async def stop(self) -> None:
        """
        Stop the process, unless it has exited: TERM to its process group, then
        KILL if it is still running after a few seconds. Then remove its run
        directory. Once only; cancelled, it still stops the process.
        """
        await self._attachment.close()

    async def stderr(self, lines: int = 20) -> str:
        """
        The last `lines` lines of its stderr, until it is stopped. Raises
        `CommandFailed` once its run directory is removed.
        """
        return await self._host.run(
            ["tail", "-n", str(lines), "--", str(self.run_dir / _STDERR_NAME)]
        )


async def start(
    host: RemoteHost,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: PurePosixPath | None = None,
) -> Process:
    """
    Start `argv` detached on `host` (see the module), with `env` added to its
    environment, in `cwd` (by default the user's home directory). Not through a
    login shell: as `RemoteHost.run` runs commands. Attached to the host until
    stopped. Raises `ValueError` for an empty `argv` or a name in `env` that
    isn't a shell variable's.
    """
    if not argv:
        raise ValueError("a process needs a command")
    env = dict(env or {})
    if invalid := [name for name in env if not _ENV_NAME.fullmatch(name)]:
        raise ValueError(f"not names of environment variables: {invalid!r}")
    watcher = host.process_watcher or ProcessWatcher(host)
    attachment = host.attachments.attach()
    try:
        return await _start(host, watcher, attachment, list(argv), env, cwd)
    except BaseException:
        # Before raising: nothing else holds the process to stop it.
        await attachment.close()
        raise


async def _start(
    host: RemoteHost,
    watcher: ProcessWatcher,
    attachment: Attachment,
    argv: list[str],
    env: dict[str, str],
    cwd: PurePosixPath | None,
) -> Process:
    sftp = await watcher.sftp()
    run_id = secrets.token_hex(8)
    run_dir = _RUN_ROOT / f"{_RUN_PREFIX}{run_id}"

    async def remove_run_directory() -> None:
        await host.run(["rm", "-rf", "--", str(run_dir)])

    # Fails if the path exists: only a directory made here is the user's alone.
    await finish(
        "create the run directory",
        sftp.mkdir(run_dir, asyncssh.SFTPAttrs(permissions=0o700)),
        then=lambda _: attachment.push(
            "remove the run directory", remove_run_directory
        ),
    )
    status_path = run_dir / _STATUS_NAME
    exited = watcher.watch(status_path)

    async def forget() -> None:
        watcher.forget(status_path)

    attachment.push("stop watching the process", forget)
    await _write(
        sftp, run_dir / _LAUNCH_NAME, launch_script(run_dir, argv, env=env, cwd=cwd)
    )

    pgids: list[int] = []

    def started(output: str) -> None:
        if (pgid := _process_group(output)) is None:
            return
        pgids.append(pgid)
        attachment.push(
            "stop the process", functools.partial(_stop, host, run_dir, pgid, exited)
        )

    output = await finish(
        "start the process",
        host.run(["setsid", "-f", "bash", str(run_dir / _LAUNCH_NAME)]),
        then=started,
    )
    if not pgids:
        raise RuntimeError(
            f"the launch script didn't say which process it started: {output!r}"
        )
    [pgid] = pgids
    record = {
        "run_id": run_id,
        "pgid": pgid,
        "executable": argv[0],
        "started": datetime.now(UTC).isoformat(),
        "status_file": str(status_path),
    }
    await _write(sftp, run_dir / _RECORD_NAME, json.dumps(record))
    status = ProcessStatus(run_id=run_id, executable=argv[0])
    attachment.report(status)
    exited.add_done_callback(functools.partial(_report_exit, attachment, status))
    logger.debug(
        t"Process started: run_id={run_id:detail} pgid={pgid:detail} "
        t"executable={argv[0]:detail} run_dir={run_dir:path}"
    )
    return Process(run_dir, pgid, exited, host, attachment)


def launch_script(
    run_dir: PurePosixPath,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: PurePosixPath | None,
) -> str:
    """The bash script that runs `argv` detached in `run_dir` (see the module)."""
    command = f"exec {shlex.join(argv)}"
    if cwd is not None:
        command = f"cd -- {shlex.quote(str(cwd))} && {command}"
    return "\n".join(
        [
            "# Written by EzHPCy: runs one process detached (see ezhpcy.process).",
            f"run_dir={shlex.quote(str(run_dir))}",
            # Bash outlives a signal to the group, to write the status; the
            # command doesn't inherit a trap with a command.
            "trap true HUP INT TERM",
            # Its PID is its process group's: `setsid` made it lead one.
            "printf '%s\\n' \"$$\"",
            # Gives up the session's output, so that the session ends.
            'exec </dev/null >"$run_dir/out" 2>"$run_dir/err"',
            *(f"export {name}={shlex.quote(value)}" for name, value in env.items()),
            # In the foreground, not with `exec`, to report how it ended; in a
            # subshell that is stopped already if it was stopped before it began.
            "(",
            f'    if [ -e "$run_dir/{_STOP_NAME}" ]; then exit {_TERMINATED_STATUS}; fi',
            f"    {command}",
            ")",
            "status=$?",
            'printf \'%s\\n\' "$status" >"$run_dir/status.tmp"',
            'mv -f -- "$run_dir/status.tmp" "$run_dir/status"',
            "",
        ]
    )


class ProcessWatcher:
    """
    Polls the status files of one host's processes, over one SFTP session, about
    once a second; their run directories are made through it too. Made by the
    first process started on the host, and attached to the host before that
    process, so that it is closed after them all.
    """

    def __init__(self, host: RemoteHost) -> None:
        loop = asyncio.get_running_loop()
        self._sftp: asyncio.Future[asyncssh.SFTPClient] = loop.create_future()
        self._pending: dict[PurePosixPath, asyncio.Future[int]] = {}
        # Why it stopped watching, once it has.
        self._error: Exception | None = None
        attachment = host.attachments.attach()
        host.process_watcher = self
        self._task = asyncio.create_task(self._run(host), name="process watcher")

        async def stop() -> None:
            self._task.cancel()
            await asyncio.wait([self._task])

        attachment.push("stop watching processes", stop)

    async def sftp(self) -> asyncssh.SFTPClient:
        """Its SFTP session, once open; raises if it couldn't be opened."""
        return await asyncio.shield(self._sftp)

    def watch(self, status_path: PurePosixPath) -> asyncio.Future[int]:
        """
        Watch for the status file `status_path`: the future resolves to the
        exit status it holds once it exists.
        """
        exited = asyncio.get_running_loop().create_future()
        if self._error is not None:
            exited.set_exception(self._error)
        else:
            self._pending[status_path] = exited
        return exited

    def forget(self, status_path: PurePosixPath) -> None:
        """Stop watching `status_path`; its future fails unless it resolved."""
        exited = self._pending.pop(status_path, None)
        if exited is not None and not exited.done():
            exited.set_exception(
                RuntimeError("the process was stopped before it said how it ended")
            )

    async def _run(self, host: RemoteHost) -> None:
        try:
            async with host.sftp() as sftp:
                self._sftp.set_result(sftp)
                while True:
                    await asyncio.sleep(_STATUS_POLL_SECONDS)
                    await asyncio.gather(
                        *(self._check(sftp, path) for path in list(self._pending))
                    )
        except (asyncssh.Error, OSError) as error:
            logger.debug(t"Stopped watching processes: error={error!r}")
            self._stopped(error)
        finally:
            self._stopped(RuntimeError("the host's connection is closed"))

    async def _check(
        self, sftp: asyncssh.SFTPClient, status_path: PurePosixPath
    ) -> None:
        try:
            async with sftp.open(status_path) as file:
                content = str(await file.read())
        except asyncssh.SFTPNoSuchFile:
            return
        exited = self._pending.pop(status_path, None)
        if exited is None or exited.done():
            return
        try:
            exited.set_result(int(content.strip()))
        except ValueError:
            exited.set_exception(
                RuntimeError(f"malformed exit status in {status_path}: {content!r}")
            )

    def _stopped(self, error: Exception) -> None:
        if self._error is None:
            self._error = error
        if not self._sftp.done():
            self._sftp.set_exception(error)
        for exited in self._pending.values():
            if not exited.done():
                exited.set_exception(error)
        self._pending.clear()


async def list_runs(host: RemoteHost) -> dict[PurePosixPath, dict[str, Any]]:
    """
    The records of the processes on `host` whose run directories the user can
    read, by run directory: running or not, from this client or another. Run
    directories without a readable record are left out.
    """
    sftp = await (host.process_watcher or ProcessWatcher(host)).sftp()
    runs: dict[PurePosixPath, dict[str, Any]] = {}
    for name in await sftp.listdir(_RUN_ROOT):
        if not name.startswith(_RUN_PREFIX):
            continue
        run_dir = _RUN_ROOT / name
        try:
            async with sftp.open(run_dir / _RECORD_NAME) as file:
                runs[run_dir] = json.loads(str(await file.read()))
        except (asyncssh.SFTPError, ValueError) as error:
            logger.debug(t"Not a run directory of ours: {run_dir:path} {error!r}")
    return runs


async def _write(sftp: asyncssh.SFTPClient, path: PurePosixPath, content: str) -> None:
    """Write a file only the user can read."""
    async with sftp.open(
        path, "w", attrs=asyncssh.SFTPAttrs(permissions=0o600)
    ) as file:
        await file.write(content)


def _process_group(output: str) -> int | None:
    """The process group the launch script printed, if it printed one."""
    first, _, _ = output.partition("\n")
    return int(first) if first.strip().isdigit() else None


async def _stop(
    host: RemoteHost, run_dir: PurePosixPath, pgid: int, exited: asyncio.Future[int]
) -> None:
    """
    Stop the process in `run_dir`, whose group is `pgid`, unless it has exited:
    TERM, then KILL if it hasn't exited within the grace period.
    """
    if exited.done():
        return
    await _signal(host, run_dir, "TERM", pgid)
    try:
        async with asyncio.timeout(_STOP_GRACE_SECONDS):
            await asyncio.shield(exited)
    except TimeoutError:
        logger.debug(
            t"Process didn't exit after TERM: pgid={pgid:detail} "
            t"grace_seconds={_STOP_GRACE_SECONDS:detail:g}"
        )
        await _signal(host, run_dir, "KILL", pgid)
        # The launch script is killed too, so it can't say.
        if not exited.done():
            exited.set_result(_KILLED_STATUS)


async def _signal(
    host: RemoteHost, run_dir: PurePosixPath, signal: str, pgid: int
) -> None:
    try:
        await host.run(
            ["bash", "-c", STOP_SCRIPT, "stop", str(run_dir), signal, str(pgid)]
        )
    except CommandFailed as error:
        # It has exited meanwhile.
        logger.debug(t"Could not signal process group {pgid:detail}: {error}")


def _report_exit(
    attachment: Attachment, status: ProcessStatus, exited: asyncio.Future[int]
) -> None:
    if exited.cancelled() or exited.exception() is not None:
        return
    exit_status = exited.result()
    logger.debug(
        t"Process exited: run_id={status.run_id:detail} "
        t"exit_status={exit_status:detail}"
    )
    attachment.report(replace(status, exit_status=exit_status))
