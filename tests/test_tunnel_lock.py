"""Tests for `ezhpcy.tunnel.lock`: one running tunnel per alias."""

import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
from contextlib import ExitStack
from pathlib import Path

import pytest

from ezhpcy.tunnel.lock import (
    LOCK_DIRECTORY_NAME,
    AliasInUse,
    alias_lock,
    alias_locked,
    lock_path,
)

# How long the lock holder may take to start or to end once killed.
PROCESS_SECONDS = 30

# The runtime directory isn't read from the environment, so set it in-process.
_HOLDER_PROGRAM = textwrap.dedent(
    """
    import os
    import sys
    from pathlib import Path
    from ezhpcy.config import config
    from ezhpcy.tunnel.lock import alias_lock

    config.local_file.runtime_dir = Path(sys.argv[1])
    with alias_lock("gpu"):
        print(os.getpid(), flush=True)
        sys.stdin.read()
    """
)
# `TerminateProcess` on Windows; nothing in the holder runs either way.
_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def test_alias_lock_is_held_until_released():
    assert not alias_locked("gpu")

    with alias_lock("gpu"):
        assert alias_locked("gpu")

    assert not alias_locked("gpu")


def test_a_second_lock_for_a_held_alias_is_refused():
    with alias_lock("gpu"):
        with (
            pytest.raises(AliasInUse, match=r"^a tunnel is already running for gpu$"),
            alias_lock("gpu"),
        ):
            pass

        # Closing the refused handle leaves the first one's lock alone.
        assert alias_locked("gpu")


def test_aliases_are_locked_separately():
    with alias_lock("gpu"):
        assert not alias_locked("cpu")
        with alias_lock("cpu"):
            assert alias_locked("cpu")


def test_alias_lock_can_be_taken_again_after_release():
    with alias_lock("gpu"):
        pass

    with alias_lock("gpu"):
        assert alias_locked("gpu")


def test_alias_lock_can_be_released_from_another_thread():
    """As the tunnel does, so that a release that hangs can be given up on."""
    stack = ExitStack()
    stack.enter_context(alias_lock("gpu"))

    releasing = threading.Thread(target=stack.close)
    releasing.start()
    releasing.join(PROCESS_SECONDS)

    assert not alias_locked("gpu")


def test_alias_lock_is_freed_when_its_holder_is_killed(isolated_runtime_dir: Path):
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_PROGRAM, str(isolated_runtime_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        holder_pid = int(process.stdout.readline())
        assert alias_locked("gpu")

        # Not `process.kill()`: in a Windows venv, `process` is a launcher whose
        # interpreter, the holder, ends a moment after it.
        os.kill(holder_pid, _KILL)
        # The launcher exits after the holder.
        process.wait(PROCESS_SECONDS)

        assert not alias_locked("gpu")
    finally:
        process.kill()
        process.communicate(timeout=PROCESS_SECONDS)


def test_alias_locked_creates_nothing(isolated_runtime_dir: Path):
    assert not alias_locked("gpu")

    assert not (isolated_runtime_dir / LOCK_DIRECTORY_NAME).exists()


def test_alias_lock_lives_in_the_runtime_directory(isolated_runtime_dir: Path):
    with alias_lock("gpu"):
        pass

    assert lock_path("gpu") == isolated_runtime_dir / LOCK_DIRECTORY_NAME / "gpu.lock"
    assert lock_path("gpu").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_alias_lock_directory_is_private(isolated_runtime_dir: Path):
    with alias_lock("gpu"):
        pass

    directory = isolated_runtime_dir / LOCK_DIRECTORY_NAME
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("alias", ["../gpu", "gpu/../../etc", "-gpu", ""])
def test_unsafe_aliases_are_rejected(alias: str, isolated_runtime_dir: Path):
    with pytest.raises(ValueError), alias_lock(alias):
        pass
    with pytest.raises(ValueError):
        alias_locked(alias)

    assert not (isolated_runtime_dir / LOCK_DIRECTORY_NAME).exists()
