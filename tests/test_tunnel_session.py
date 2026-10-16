"""
`ezhpcy tunnel` end to end: the session against the in-process SSH server, with
the worker played by the tests. How each failure shuts the tunnel down is in
`test_tunnel_shutdown.py`.
"""

import asyncio
import functools
import io
import logging
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ezhpcy.cli import tunnel as tunnel_cli
from ezhpcy.cli.utils import ssh as cli_ssh
from ezhpcy.ipc.client import connect, relay_proxy_stdio
from ezhpcy.ipc.descriptor import TunnelDescriptor, descriptor_path
from ezhpcy.scheduler.base import JobState
from ezhpcy.tunnel import session, ssh_config, worker
from tests.support.cli import invoke
from tests.support.sshserver import Invocation, SSHTestServer, reply
from tests.support.tunnel import (
    BANNER,
    JOB_ID,
    RUNNING,
    FakeScheduler,
    banner,
    banner_then_echo,
    configure_tunnel,
    control_dirs,
    serve_interactive_job,
    serve_login_shell,
    snapshot,
    tunnel_log,
    use_scheduler,
    write_worker_record,
)

# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5
# How long a step takes to finish after Ctrl+C, so that the stop arrives meanwhile.
STOP_SECONDS = 0.1


def run_proxy(alias: str, stdin: bytes) -> bytes:
    """Run `ezhpcy proxy`'s relay with `stdin`; return what it wrote to stdout."""
    stdout = io.BytesIO()
    with connect(TunnelDescriptor.load(alias), timeout=WAIT_SECONDS) as stream:
        stream.settimeout(WAIT_SECONDS)
        relay_proxy_stdio(stream, io.BytesIO(stdin), stdout)
    return stdout.getvalue()


def bjobs(running: threading.Event, done: threading.Event):
    """LSF's `bjobs`: pending until `running`, then running until `done`."""

    def handler(invocation: Invocation) -> None:
        if done.is_set():
            state, exit_code = "DONE", "0"
        elif running.is_set():
            state, exit_code = "RUN", "-"
        else:
            state, exit_code = "PEND", "-"
        host = "-" if state == "PEND" else "127.0.0.1"
        invocation.stdout.write(f"{JOB_ID}|{state}|{host}|{exit_code}\n")

    return handler


def test_a_tunnel_relays_proxies_to_the_worker(
    login: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
) -> None:
    worker_port = login.listen(banner_then_echo)
    running, done = threading.Event(), threading.Event()
    submissions: list[list[str]] = []
    worker_commands: list[str] = []

    async def bsub(invocation: Invocation) -> None:
        assert invocation.term_type is not None
        submissions.append(invocation.argv)
        invocation.stdout.write(f"Job <{JOB_ID}> is submitted to queue <normal>.\n")
        worker_commands.append(await invocation.stdin.readline())
        # The worker's sshd listens, and says so.
        write_worker_record(login, "ready", str(worker_port))
        running.set()
        invocation.stdout.write("<<Starting on 127.0.0.1>>\n")
        while await invocation.stdin.readline():
            pass

    serve_login_shell(login, bsub=bsub, bjobs=bjobs(running, done), bkill=reply())
    configure_tunnel(
        monkeypatch, login, resources={"queue": "normal", "cores": 4, "memory": "2GiB"}
    )
    monkeypatch.setattr(ssh_config, "check_host_resolution", lambda _host: None)
    active_config = isolated_runtime_dir / "ssh-config" / "active-scratch.conf"
    published: list[bool] = []
    outputs: list[bytes] = []

    def publish_then_connect(*args: object, **kwargs: object) -> None:
        ssh_config.publish_host(*args, **kwargs)  # type: ignore[arg-type]
        published.append(active_config.exists())
        # As OpenSSH runs `ezhpcy proxy scratch`, twice.
        outputs.extend(run_proxy("scratch", data) for data in (b"hello", b"again"))
        # Then the job ends.
        done.set()

    monkeypatch.setattr(session, "publish_host", publish_then_connect)
    previous_handler = signal.getsignal(signal.SIGINT)

    with tunnel_log() as log:
        result = invoke(
            ["tunnel", "base", "--alias", "scratch", "--worker-port", str(worker_port)]
        )

    assert result.exit_code == 0, result
    assert outputs == [BANNER + b"worker:hello", BANNER + b"worker:again"]
    # The interactive job's output after its ID reaches the user.
    assert "<<Starting on 127.0.0.1>>" in result.stderr

    [submission] = submissions
    assert submission[:2] == ["bsub", "-Is"]
    assert submission[submission.index("-q") + 1] == "normal"
    assert submission[submission.index("-n") + 1] == "4"
    assert "rusage[mem=2048MB]" in submission
    [worker_command] = worker_commands
    assert worker_command.startswith("cd /home/alice/.cache/ezhpcy/")
    assert " && exec env PIXI_HOME=" in worker_command

    assert (
        log.messages(logging.INFO).count(
            "Stopping tunnel: worker job 42 ended in scheduler state DONE with exit code 0"
        )
        == 1
    )
    # The job ended by itself, so it isn't cancelled.
    assert not any(
        argv[:2] == ["bash", "-lc"] and argv[2].startswith("bkill")
        for argv in login.commands_run
    )

    # The Host block was there while serving, and everything is cleaned up.
    assert published == [True]
    assert not active_config.exists()
    assert not descriptor_path("scratch").exists()
    assert control_dirs(login) == []
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_an_lsf_batch_tunnel_sends_the_worker_script_to_bsub(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner(BANNER))
    running, done = threading.Event(), threading.Event()
    scripts: list[str] = []

    async def bsub(invocation: Invocation) -> None:
        assert invocation.term_type is None
        scripts.append(await invocation.stdin.read())
        write_worker_record(login, "ready", str(worker_port))
        running.set()
        invocation.stdout.write(f"Job <{JOB_ID}> is submitted to queue <normal>.\n")

    serve_login_shell(login, bsub=bsub, bjobs=bjobs(running, done), bkill=reply())
    configure_tunnel(
        monkeypatch, login, scheduler={"type": "LSF", "submission_mode": "batch"}
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: done.set())

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    [script] = scripts
    assert script.startswith("#!/usr/bin/env bash\nexec env PIXI_HOME=")
    assert "bash -s --" in script
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 " in script
    assert log.starting("Tunnel ready for base via worker 127.0.0.1:")


def test_keepalive_loss_while_serving_stops_the_tunnel(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner(BANNER))
    monkeypatch.setattr(
        cli_ssh,
        "connect_interactive",
        functools.partial(
            cli_ssh.connect_interactive, keepalive_interval=0.1, keepalive_count_max=2
        ),
    )

    def unplug() -> object:
        login.blackhole()
        return RUNNING

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    schedulers = use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, unplug, RUNNING],
            submitted=lambda _spec: write_worker_record(
                login, "ready", str(worker_port)
            ),
        ),
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: None)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 1, result
    assert log.command_errors() == [
        (
            "Tunnel stopped: login-node SSH connection lost: Server not responding "
            "to keepalive"
        )
    ]
    [scheduler] = schedulers
    # The fake doesn't need the connection; a real `bkill` would fail.
    assert scheduler.cancelled == [JOB_ID]


@pytest.mark.parametrize(
    "interrupt",
    [
        pytest.param(signal.SIGINT, id="SIGINT"),
        # Ctrl+Break, on Windows only.
        pytest.param(
            getattr(signal, "SIGBREAK", None),
            marks=pytest.mark.skipif(
                not hasattr(signal, "SIGBREAK"), reason="Windows only"
            ),
            id="SIGBREAK",
        ),
    ],
)
def test_a_second_interrupt_quits_without_cleaning_up(
    login: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: signal.Signals,
) -> None:
    worker_port = login.listen(banner(BANNER))
    exits: list[int] = []
    # In place of ending the test run; the cleanup then simply carries on.
    monkeypatch.setattr(tunnel_cli.os, "_exit", exits.append)

    def press() -> object:
        signal.raise_signal(interrupt)
        return RUNNING

    async def press_again() -> None:
        signal.raise_signal(interrupt)

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, press, RUNNING],
            submitted=lambda _spec: write_worker_record(
                login, "ready", str(worker_port)
            ),
            cancelling=press_again,
        ),
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: None)
    previous_handler = signal.getsignal(interrupt)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    assert exits == [130]
    assert "Stopping tunnel: Ctrl+C" in log.messages()
    assert "Press Ctrl+C again to quit without cleaning up." in log.messages()
    assert log.starting("Quitting without cleaning up.")
    assert signal.getsignal(interrupt) is previous_handler


def test_a_cleanup_step_that_hangs_is_given_up_on(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_port = login.listen(banner(BANNER))
    monkeypatch.setattr(session, "_CLEANUP_SECONDS", 0.2)

    async def hang() -> None:
        await asyncio.Event().wait()

    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING, snapshot(JobState.UNKNOWN, "UNKWN")],
            submitted=lambda _spec: write_worker_record(
                login, "ready", str(worker_port)
            ),
            cancelling=hang,
        ),
    )
    monkeypatch.setattr(session, "publish_host", lambda *_args, **_kwargs: None)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 1, result
    assert log.starting("Could not cancel worker job 42: no answer within 0.2 seconds.")
    # Cleanup carried on after the job.
    assert control_dirs(login) == []


@pytest.mark.parametrize("hangs", [False, True], ids=["finishes", "hangs"])
def test_ctrl_c_during_batch_submission_lets_it_finish(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch, hangs: bool
) -> None:
    """Cancelled midway, `qsub` could queue the job without returning its ID."""
    monkeypatch.setattr(session, "_CLEANUP_SECONDS", 0.2)

    async def press() -> None:
        signal.raise_signal(signal.SIGINT)
        await (asyncio.Event().wait() if hangs else asyncio.sleep(STOP_SECONDS))

    configure_tunnel(
        monkeypatch, login, scheduler={"type": "PBS", "submission_mode": "batch"}
    )
    schedulers = use_scheduler(
        monkeypatch, lambda host: FakeScheduler(host, [RUNNING], submitting=press)
    )

    with tunnel_log() as log:
        result = invoke(["tunnel", "base"])

    assert result.exit_code == 0, result
    assert "Stopping tunnel: Ctrl+C" in log.messages()
    [scheduler] = schedulers
    if hangs:
        # Given up on, with a warning.
        assert scheduler.cancelled == []
        assert log.starting(
            "Gave up waiting to submit the worker job after 0.2 seconds"
        )
    else:
        assert scheduler.cancelled == [JOB_ID]
    assert control_dirs(login) == []


def _stop_from_thread() -> None:
    """Press Ctrl+C from a step run in a thread, and give the stop time to arrive."""
    signal.raise_signal(signal.SIGINT)
    time.sleep(STOP_SECONDS)


def _stop_after_creating_the_control_directory(
    monkeypatch: pytest.MonkeyPatch, _published: list[str]
) -> None:
    create = worker.WorkerControl.create

    async def create_then_stop(control: worker.WorkerControl) -> None:
        await create(control)
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(STOP_SECONDS)

    monkeypatch.setattr(worker.WorkerControl, "create", create_then_stop)


def _stop_before_publishing_the_descriptor(
    monkeypatch: pytest.MonkeyPatch, _published: list[str]
) -> None:
    publish = TunnelDescriptor.publish

    def stop_then_publish(descriptor: TunnelDescriptor) -> None:
        _stop_from_thread()
        publish(descriptor)

    monkeypatch.setattr(TunnelDescriptor, "publish", stop_then_publish)


def _stop_before_publishing_the_host_block(
    monkeypatch: pytest.MonkeyPatch, published: list[str]
) -> None:
    def stop_then_publish(host: ssh_config.WorkerHost, **_kwargs: object) -> None:
        _stop_from_thread()
        published.append(host.alias)

    monkeypatch.setattr(session, "publish_host", stop_then_publish)


@pytest.mark.parametrize(
    "stop_during_step",
    [
        _stop_after_creating_the_control_directory,
        _stop_before_publishing_the_descriptor,
        _stop_before_publishing_the_host_block,
    ],
    ids=["control-directory", "descriptor", "host-block"],
)
def test_ctrl_c_during_a_step_still_cleans_up_after_it(
    login: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    stop_during_step: Callable[[pytest.MonkeyPatch, list[str]], None],
) -> None:
    """Cancelled midway, the step would still take effect, with no cleanup for it."""
    worker_port = login.listen(banner(BANNER))
    configure_tunnel(monkeypatch, login)
    serve_interactive_job(login)
    use_scheduler(
        monkeypatch,
        lambda host: FakeScheduler(
            host,
            [RUNNING],
            submitted=lambda _spec: write_worker_record(
                login, "ready", str(worker_port)
            ),
        ),
    )
    published: list[str] = []
    withdrawn: list[str] = []
    monkeypatch.setattr(
        session, "publish_host", lambda host, **_kwargs: published.append(host.alias)
    )
    monkeypatch.setattr(
        session, "withdraw_host", lambda host, **_kwargs: withdrawn.append(host.alias)
    )
    stop_during_step(monkeypatch, published)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(worker_port)])

    assert result.exit_code == 0, result
    assert "Stopping tunnel: Ctrl+C" in log.messages()
    assert control_dirs(login) == []
    assert not descriptor_path("base").exists()
    assert published == withdrawn


def test_ctrl_c_at_a_prompt_exits_130(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    configure_tunnel(monkeypatch, login)
    monkeypatch.setattr(cli_ssh, "connect_interactive", interrupted)

    result = invoke(["tunnel", "base"])

    assert result.exit_code == 130, result


def test_a_failed_login_is_reported(
    login: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    login.auth.client_keys = []
    configure_tunnel(monkeypatch, login)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base"])

    assert result.exit_code == 1, result
    [error] = log.command_errors()
    assert error.startswith("Could not start tunnel: Permission denied")
