import hashlib
import os
import subprocess
from contextlib import contextmanager
from pathlib import PurePosixPath

import pytest

from ezhpcy.config import RemoteFileConfig
from ezhpcy.constants import OPENSSH_MATCHSPEC
from ezhpcy.worker_payload import (
    WorkerPayloadError,
    ensure_worker_payload,
    render_worker_payload,
    require_worker_payload,
    worker_payload_path,
)


def require_bash() -> None:
    result = subprocess.run(["bash", "-c", "exit 0"], capture_output=True)
    if result.returncode != 0:
        pytest.skip("a functional Bash installation is required")


def remote_file_config() -> RemoteFileConfig:
    return RemoteFileConfig(
        cache_dir=PurePosixPath("/home/alice/.cache"),
        config_dir=PurePosixPath("/home/alice/.config"),
        data_dir=PurePosixPath("/home/alice/.local/share"),
        runtime_dir=PurePosixPath("/tmp/ezhpcy-1000"),
    )


class StubSFTP:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = files or {}
        self.directories: list[tuple[str, int]] = []
        self.chmods: list[tuple[str, int]] = []
        self.writes: list[str] = []
        self.renames: list[tuple[str, str]] = []

    def mkdir(self, path, mode=0o777, **_kwargs) -> None:
        self.directories.append((str(path), mode))

    def chmod(self, path: str, mode: int) -> None:
        self.chmods.append((path, mode))

    def read_bytes(self, path) -> bytes:
        try:
            return self.files[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None

    def write_bytes(self, path, content: bytes) -> int:
        self.files[str(path)] = content
        self.writes.append(str(path))
        return len(content)

    def posix_rename(self, source: str, destination: str) -> None:
        self.files[destination] = self.files.pop(source)
        self.renames.append((source, destination))

    def remove(self, path: str) -> None:
        try:
            del self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None


class StubSSH:
    def __init__(self, sftp: StubSFTP) -> None:
        self.sftp = sftp

    @contextmanager
    def sftp_client(self):
        yield self.sftp


def test_rendered_payload_is_lf_normalized_and_valid_shell(tmp_path) -> None:
    require_bash()
    payload = render_worker_payload()
    script = tmp_path / "ssh-serve"
    script.write_bytes(payload)

    assert b"\r" not in payload
    assert payload.endswith(b"\n")
    assert OPENSSH_MATCHSPEC.encode() in payload
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_worker_payload_path_hashes_the_exact_rendered_bytes() -> None:
    payload = render_worker_payload()
    digest = hashlib.sha256(payload).hexdigest()

    assert worker_payload_path(remote_file_config(), payload) == PurePosixPath(
        f"/home/alice/.local/share/ezhpcy/payloads/{digest}/ssh-serve"
    )


@pytest.mark.parametrize("existing", [None, b"corrupted"])
def test_ensure_worker_payload_atomically_uploads_missing_or_corrupt_content(
    existing: bytes | None,
) -> None:
    payload = render_worker_payload()
    remote_path = worker_payload_path(remote_file_config(), payload)
    initial = {str(remote_path): existing} if existing is not None else {}
    sftp = StubSFTP(initial)

    resolved = ensure_worker_payload(StubSSH(sftp), remote_file_config())  # type: ignore[arg-type]

    assert resolved == remote_path
    assert sftp.files[str(remote_path)] == payload
    assert len(sftp.writes) == 1
    temporary_path = sftp.writes[0]
    assert temporary_path.startswith(f"{remote_path.parent}/.ssh-serve.")
    assert sftp.renames == [(temporary_path, str(remote_path))]
    assert (temporary_path, 0o755) in sftp.chmods


def test_ensure_worker_payload_reuses_verified_content() -> None:
    payload = render_worker_payload()
    remote_path = worker_payload_path(remote_file_config(), payload)
    sftp = StubSFTP({str(remote_path): payload})

    resolved = ensure_worker_payload(StubSSH(sftp), remote_file_config())  # type: ignore[arg-type]

    assert resolved == remote_path
    assert sftp.writes == []
    assert sftp.renames == []
    assert (str(remote_path), 0o755) in sftp.chmods


def test_require_worker_payload_is_read_only() -> None:
    payload = render_worker_payload()
    remote_path = worker_payload_path(remote_file_config(), payload)
    sftp = StubSFTP({str(remote_path): payload})

    resolved = require_worker_payload(StubSSH(sftp), remote_file_config())  # type: ignore[arg-type]

    assert resolved == remote_path
    assert sftp.writes == []
    assert sftp.chmods == []


@pytest.mark.parametrize("existing", [None, b"corrupted"])
def test_require_worker_payload_rejects_missing_or_corrupt_content(
    existing: bytes | None,
) -> None:
    remote_path = worker_payload_path(remote_file_config())
    files = {str(remote_path): existing} if existing is not None else {}

    with pytest.raises(WorkerPayloadError, match="tunnel provision"):
        require_worker_payload(  # type: ignore[arg-type]
            StubSSH(StubSFTP(files)), remote_file_config()
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ([], "Usage: ssh-serve PORT"),
        (["not-a-port"], "must be numeric"),
        (["1023"], "must be between 1024 and 65535"),
        (["65536"], "must be between 1024 and 65535"),
    ],
)
def test_worker_payload_rejects_invalid_ports(tmp_path, arguments, message) -> None:
    require_bash()
    script = tmp_path / "ssh-serve"
    script.write_bytes(render_worker_payload())

    result = subprocess.run(
        ["bash", str(script), *arguments],
        capture_output=True,
        text=True,
        env={**os.environ, "PBS_JOBID": "42"},
    )

    assert result.returncode == 2
    assert message in result.stderr


def test_worker_payload_requires_a_compute_allocation(tmp_path) -> None:
    require_bash()
    script = tmp_path / "ssh-serve"
    script.write_bytes(render_worker_payload())
    environment = os.environ.copy()
    for name in ("LSB_JOBID", "LSF_ENVDIR", "PBS_JOBID"):
        environment.pop(name, None)

    result = subprocess.run(
        ["bash", str(script), "23456"],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "inside an LSF or PBS compute allocation" in result.stderr


def test_worker_payload_validates_then_executes_sshd_through_pixi(tmp_path) -> None:
    require_bash()
    data_home = tmp_path / "data"
    cache_home = tmp_path / "cache"
    config_home = tmp_path / "config"
    runtime_home = tmp_path / "runtime"
    pixi = data_home / "ezhpcy" / "pixi_home" / "bin" / "pixi"
    sshd_config = config_home / "ezhpcy" / "ssh" / "sshd_config"
    capture = tmp_path / "pixi-arguments"
    pixi.parent.mkdir(parents=True)
    sshd_config.parent.mkdir(parents=True)
    sshd_config.write_text("test config\n", encoding="utf-8")
    pixi.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE"\n', encoding="utf-8")
    pixi.chmod(0o755)
    script = tmp_path / "ssh-serve"
    script.write_bytes(render_worker_payload())

    result = subprocess.run(
        ["bash", str(script), "23456"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PBS_JOBID": "42",
            "XDG_DATA_HOME": data_home.as_posix(),
            "XDG_CACHE_HOME": cache_home.as_posix(),
            "XDG_CONFIG_HOME": config_home.as_posix(),
            "XDG_RUNTIME_DIR": runtime_home.as_posix(),
            "CAPTURE": capture.as_posix(),
        },
    )

    assert result.returncode == 0, result.stderr
    validation, serving = capture.read_text(encoding="utf-8").splitlines()
    assert validation.startswith(f"exec --spec={OPENSSH_MATCHSPEC} sh -c")
    assert "sshd -t" in validation
    assert "-p 23456 -o ListenAddress=0.0.0.0" in validation
    assert serving.startswith(f"exec --spec={OPENSSH_MATCHSPEC} sh -c")
    assert "sshd -D -e" in serving
    assert runtime_home.joinpath("ezhpcy").stat().st_mode & 0o777 == 0o700
