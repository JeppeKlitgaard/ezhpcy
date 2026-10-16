"""Tests for the worker's local SSH credentials in `ezhpcy.provision_local`."""

from pathlib import Path, PurePosixPath

import asyncssh
import pytest

from ezhpcy.config import config
from ezhpcy.constants import WORKER_HOST_ALIAS
from ezhpcy.provision_local import WorkerCredentials
from ezhpcy.types import RemoteState

_CREDENTIALS = WorkerCredentials("machine-id", "alice", "login.example.com")


def _key_files(credentials: WorkerCredentials) -> list[Path]:
    return [
        credentials.client_key,
        credentials.client_public_key,
        credentials.host_key,
        credentials.host_public_key,
    ]


def test_worker_credentials_stay_where_earlier_versions_put_them() -> None:
    # Local keys must keep being found where they are (PLAN.md, "Compatibility").
    local_dir = (
        config.local_file.config_dir / "ssh" / "machine-id" / "alice@login.example.com"
    )
    credentials = WorkerCredentials("machine-id", "alice", "Login.Example.com")
    remote_state = RemoteState(
        cache_dir=PurePosixPath("/home/alice/.cache"),
        state_dir=PurePosixPath("/home/alice/.local/state"),
    )

    assert credentials.local_dir == local_dir
    assert [*_key_files(credentials), credentials.known_hosts] == [
        local_dir / "worker_client_ed25519",
        local_dir / "worker_client_ed25519.pub",
        local_dir / "ssh_host_ed25519_key",
        local_dir / "ssh_host_ed25519_key.pub",
        local_dir / "worker_known_hosts",
    ]
    remote_dir = (
        f"{remote_state.package_cache_dir()}/ssh/machine-id/alice@login.example.com"
    )
    assert str(credentials.remote_host_key(remote_state)) == (
        f"{remote_dir}/ssh_host_ed25519_key"
    )
    assert str(credentials.remote_host_public_key(remote_state)) == (
        f"{remote_dir}/ssh_host_ed25519_key.pub"
    )


def test_local_worker_keys_are_generated_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_keys = 0
    original_generate = asyncssh.generate_private_key

    def count_generated_key(*args: object, **kwargs: object) -> asyncssh.SSHKey:
        nonlocal generated_keys
        generated_keys += 1
        return original_generate(*args, **kwargs)

    monkeypatch.setattr(asyncssh, "generate_private_key", count_generated_key)

    _CREDENTIALS.ensure_local_keys()
    initial_contents = [path.read_bytes() for path in _key_files(_CREDENTIALS)]
    _CREDENTIALS.ensure_local_keys()

    assert generated_keys == 2
    assert [path.read_bytes() for path in _key_files(_CREDENTIALS)] == (
        initial_contents
    )
    assert [
        path.read_text(encoding="ascii").split(maxsplit=2)[2].strip()
        for path in (_CREDENTIALS.client_public_key, _CREDENTIALS.host_public_key)
    ] == [
        "ezhpcy worker client",
        "ezhpcy worker host",
    ]
    for private_key, public_key in (
        (_CREDENTIALS.client_key, _CREDENTIALS.client_public_key),
        (_CREDENTIALS.host_key, _CREDENTIALS.host_public_key),
    ):
        loaded_key = asyncssh.read_private_key(private_key)
        public_fields = public_key.read_text(encoding="ascii").split()
        assert public_fields == (
            loaded_key.export_public_key("openssh").decode().split()
        )


def test_existing_local_worker_keys_are_kept_and_still_load() -> None:
    # Key files as earlier versions wrote them: OpenSSH's private key format, and
    # the public key line followed by a comment.
    keys = {}
    for private_key, public_key, comment in (
        (_CREDENTIALS.client_key, _CREDENTIALS.client_public_key, "client"),
        (_CREDENTIALS.host_key, _CREDENTIALS.host_public_key, "host"),
    ):
        private_key.parent.mkdir(parents=True, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519")
        private_key.write_bytes(key.export_private_key("openssh"))
        public_line = key.export_public_key("openssh").decode().strip()
        public_key.write_text(
            f"{public_line} ezhpcy worker {comment}\n", encoding="ascii"
        )
        keys[private_key] = key
    contents = [path.read_bytes() for path in _key_files(_CREDENTIALS)]

    _CREDENTIALS.ensure_local_keys()

    assert [path.read_bytes() for path in _key_files(_CREDENTIALS)] == contents
    for private_key, key in keys.items():
        loaded = asyncssh.read_private_key(private_key)
        assert loaded.export_public_key("openssh") == key.export_public_key("openssh")


def test_local_worker_host_keys_differ_between_login_endpoints() -> None:
    login1 = WorkerCredentials("machine-id", "alice", "login1.hpc.dtu.dk")
    login2 = WorkerCredentials("machine-id", "alice", "login2.hpc.dtu.dk")

    login1.ensure_local_keys()
    login2.ensure_local_keys()

    assert login1.host_public_key.read_bytes() != login2.host_public_key.read_bytes()


def test_pinning_the_host_key_preserves_unrelated_entries() -> None:
    _CREDENTIALS.local_dir.mkdir(parents=True)
    _CREDENTIALS.host_public_key.write_text(
        "ssh-ed25519 NEW ezhpcy worker host\n", encoding="ascii"
    )
    _CREDENTIALS.known_hosts.write_text(
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 OLD\n",
        encoding="utf-8",
    )

    _CREDENTIALS.pin_host_key()

    assert _CREDENTIALS.known_hosts.read_text(encoding="utf-8") == (
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 NEW\n"
    )
