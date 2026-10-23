"""
App projects on this machine for tests, a manifest beside Pixi files, and a
fake `pixi` and shell tools for provisioning them on the test server.
"""

import re
import shlex
import shutil
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import asyncssh
import pytest

from ezhpcy.app import run as app_run
from tests.support.sshserver import Invocation, SSHTestServer, reply

PIXI_TOML = """\
[workspace]
name = "project"
channels = ["conda-forge"]
platforms = ["linux-64"]

[dependencies]
python = "3.13.*"
"""


def lock_file(
    environments: dict[str, dict[str, list[str]]] | None = None, *, version: int = 6
) -> str:
    """
    A `pixi.lock` in Pixi's layout, with these package entries (such as
    `conda: https://…` or `pypi: ./`) by environment and platform; by default,
    one conda package for `default` on linux-64.
    """
    if environments is None:
        environments = {
            "default": {
                "linux-64": [
                    "conda: https://conda.anaconda.org/conda-forge/linux-64/python-3.13.conda"
                ]
            }
        }
    lines = [f"version: {version}", "environments:"]
    entries = []
    for environment, platforms in environments.items():
        lines += [
            f"  {environment}:",
            "    channels:",
            "    - url: https://conda.anaconda.org/conda-forge/",
            "    packages:",
        ]
        for platform, packages in platforms.items():
            lines.append(f"      {platform}:")
            for entry in packages:
                lines.append(f"      - {entry}")
                entries.append(entry)
    lines.append("packages:")
    for entry in entries:
        lines += [f"- {entry}", "  sha256: 0123"]
    return "\n".join(lines) + "\n"


def write_project(
    directory: Path,
    manifest: str,
    *,
    pixi_toml: str = PIXI_TOML,
    lock: str | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    """
    Write `ezhpcy-apps.toml` with `manifest`, beside `pixi.toml` and `pixi.lock`
    (unless `lock` is `""`) and `files`; return the manifest's path.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pixi.toml").write_text(pixi_toml, encoding="utf-8")
    if lock != "":
        (directory / "pixi.lock").write_text(
            lock if lock is not None else lock_file(), encoding="utf-8"
        )
    for name, content in (files or {}).items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    path = directory / "ezhpcy-apps.toml"
    path.write_text(manifest, encoding="utf-8")
    return path


# A small valid app, to add to or change: its version's table is last, so
# that lines added to the end are its fields.
MINIMAL_APP = """\
[ezhpcy]
schema = 1

[ezhpcy.app.lab."v1.0"]
environment.pixi = "pixi.toml"
run.command = ["jupyter", "lab", "--ServerApp.sock={socket}"]
endpoint.transport = "socket"
"""

PREFIX_MARKER = "/.pixi/envs/default"


class FakePixi:
    """
    EzHPCy's Pixi on the test server. Installing it (`provision_pixi`'s `bash
    -c` script) creates its executable, counted in `pixi_installs`. `pixi
    install`, as `run_pixi` runs it (`env PIXI_HOME=… PIXI_CACHE_DIR=… pixi
    …`), writes the environment beside the manifest, or with `fail`, writes
    some of it and then fails as on a full disk; `installing` is called before
    it finishes, on the server's thread.
    """

    def __init__(self, server: SSHTestServer) -> None:
        self.server = server
        self.installs: list[str] = []
        self.cache_dirs: list[str] = []
        self.pixi_installs = 0
        self.fail = False
        self.installing: Callable[[str], None] = lambda _manifest: None
        server.command("env", self)
        server.command("bash -c", self._install_pixi)

    def __call__(self, invocation: Invocation) -> int:
        argv = invocation.argv
        self.cache_dirs.append(argv[2].removeprefix("PIXI_CACHE_DIR="))
        command, options = argv[4], argv[5:]
        # The worker infrastructure's `pixi exec` runs.
        if "--manifest-path" not in options:
            return 0
        assert command == "install", argv
        manifest = options[options.index("--manifest-path") + 1]
        self.installs.append(manifest)
        bin_dir = self.server.local_path(
            str(PurePosixPath(manifest).parent) + PREFIX_MARKER + "/bin"
        )
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        self.installing(manifest)
        if self.fail:
            invocation.stderr.write(
                "Error:   × failed to link python\n"
                "  ╰─▶ No space left on device (os error 28)\n"
            )
            return 1
        return 0

    def _install_pixi(self, invocation: Invocation) -> None:
        if match := re.search(r"\[ ! -x (.+?) \]", invocation.argv[2]):
            [path] = shlex.split(match[1])
            executable = self.server.local_path(path)
            if not executable.exists():
                self.pixi_installs += 1
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("#!/bin/sh\n", encoding="utf-8")


def quick_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check apps' readiness every 10 ms."""
    monkeypatch.setattr(app_run, "_READY_POLL_SECONDS", 0.01)


def http_app(status: int = 200, body: bytes = b"hello") -> Callable[..., Any]:
    """
    A stand-in app, for a socket (`allow_socket`) or a TCP port (`listen`): it
    answers every request with `status` and `body`, and closes.
    """

    async def serve(reader: Any, writer: Any) -> None:
        # The client may close first, e.g. a readiness check that timed out; an
        # error here would make asyncssh drop the whole connection, which an
        # app doesn't do to sshd.
        try:
            while await reader.readline() not in (b"\r\n", b"\n", b""):
                pass
            writer.write(f"HTTP/1.0 {status} Status\r\n\r\n".encode() + body)
        except OSError, asyncssh.Error:
            pass
        writer.close()

    return serve


def serve_shell_tools(server: SSHTestServer, *, free_kib: int = 1_048_576) -> None:
    """`sh -c '… rm -rf …' remove PATH…`, which removes for real, and `df -Pk`."""

    def remove(invocation: Invocation) -> None:
        for path in invocation.argv[4:]:
            local = server.local_path(path)
            if local.is_symlink() or local.is_file():
                local.unlink()
            elif local.exists():
                # As `chmod -R u+w` first: read-only directories keep their
                # files on POSIX.
                for directory, _names, _files in local.walk():
                    directory.chmod(stat.S_IRWXU)
                shutil.rmtree(local, onexc=_make_writable_and_retry)

    server.command("sh -c", remove)
    server.command(
        "df -Pk",
        reply(
            stdout=(
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"beegfs 100000000 99000000 {free_kib} 99% /work3\n"
            )
        ),
    )


def _make_writable_and_retry(function, path, _error) -> None:
    Path(path).chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    function(path)
