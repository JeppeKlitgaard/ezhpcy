import base64
import json
import os
from pathlib import Path

import pytest

from ezhpcy.ipc import IPCError, descriptor as descriptor_module
from ezhpcy.ipc.descriptor import TunnelDescriptor, descriptor_path
from ezhpcy.types import ResolvedConfig

AUTHKEY = b"a" * 32
SCHEDULER = {"type": "LSF", "submission_mode": "batch"}


def test_descriptor_path_is_keyed_by_alias(isolated_runtime_dir: Path) -> None:
    assert descriptor_path("gpu") == isolated_runtime_dir / "descriptors" / "gpu.json"


@pytest.mark.parametrize(
    "alias", ["", "../escape", "a/b", "a\b", "has space", "-leading", "wild*", "a,b"]
)
def test_descriptor_path_rejects_aliases_that_are_not_safe_file_names(
    alias: str,
) -> None:
    with pytest.raises(ValueError, match="invalid host alias"):
        descriptor_path(alias)


def test_anonymous_alias_digest_classifies_every_field() -> None:
    resolved = ResolvedConfig.model_validate(
        {
            "connection": {"user": "alice", "host": "login.example.com"},
            "scheduler": SCHEDULER,
        }
    )
    participants = resolved.model_dump(
        mode="json", exclude=ResolvedConfig._DIGEST_EXCLUDE_FIELDS
    )

    assert {
        section: frozenset(fields) for section, fields in participants.items()
    } == ResolvedConfig._DIGEST_INCLUDE_FIELDS


def test_anonymous_alias_digest_is_order_independent() -> None:
    first = ResolvedConfig.model_validate(
        {
            "connection": {"user": "alice", "host": "login.example.com"},
            "scheduler": SCHEDULER,
            "lsf": {"submission_environment": {"A": "first", "Z": "last"}},
        }
    )
    second = ResolvedConfig.model_validate(
        {
            "connection": {"host": "login.example.com", "user": "alice"},
            "scheduler": SCHEDULER,
            "lsf": {"submission_environment": {"Z": "last", "A": "first"}},
        }
    )

    assert first.descriptor_digest() == second.descriptor_digest()


def test_anonymous_alias_digest_excludes_password_sources() -> None:
    without_password = ResolvedConfig.model_validate(
        {
            "connection": {"user": "alice", "host": "login.example.com"},
            "scheduler": SCHEDULER,
        }
    )
    password = ResolvedConfig.model_validate(
        {
            "connection": {
                "user": "alice",
                "host": "login.example.com",
                "password": "secret",
                "password_prompt": False,
            },
            "scheduler": SCHEDULER,
        }
    )

    assert password.descriptor_digest() == without_password.descriptor_digest()


def test_anonymous_alias_digest_excludes_timings() -> None:
    connection = {"user": "alice", "host": "login.example.com"}
    default = ResolvedConfig.model_validate(
        {"connection": connection, "scheduler": SCHEDULER}
    )
    changed = ResolvedConfig.model_validate(
        {
            "connection": {**connection, "ssh_keepalive_interval_seconds": 75},
            "scheduler": SCHEDULER,
            "timings": {"queue_timeout_seconds": 60},
        }
    )

    assert default.descriptor_digest() == changed.descriptor_digest()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("connection", "user", "bob"),
        ("connection", "host", "other.example.com"),
        ("resources", "queue", "cpu"),
        ("resources", "cores", 16),
        ("scheduler", "type", "PBS"),
        ("scheduler", "submission_mode", "interactive"),
        ("scheduler", "interactive_submission_command", ["/site/bin/interactive"]),
        ("pbs", "command_directory", "/opt/pbspro/bin"),
    ],
)
def test_anonymous_alias_digest_includes_job_configuration(
    section: str, field: str, value: object
) -> None:
    base_values: dict[str, dict[str, object]] = {
        "connection": {"user": "alice", "host": "login.example.com"},
        "scheduler": SCHEDULER,
        "resources": {"queue": "gpu", "cores": 8},
    }
    changed_values = {
        **base_values,
        section: {**base_values.get(section, {}), field: value},
    }

    assert (
        ResolvedConfig.model_validate(base_values).descriptor_digest()
        != ResolvedConfig.model_validate(changed_values).descriptor_digest()
    )


@pytest.mark.parametrize("debug", [False, True])
def test_descriptor_is_published_and_loaded_by_alias(debug: bool) -> None:
    descriptor = TunnelDescriptor(
        "ezhpcy-0123456789ab", port=4321, authkey=AUTHKEY, debug=debug
    )
    descriptor.publish()

    assert TunnelDescriptor.load("ezhpcy-0123456789ab") == descriptor


def test_descriptor_file_format_is_stable() -> None:
    """Old and new tunnels and proxies all read this format."""
    descriptor = TunnelDescriptor("test", port=4321, authkey=AUTHKEY)
    descriptor.publish()

    assert json.loads(descriptor.path.read_text(encoding="utf-8")) == {
        "version": 2,
        "host": "127.0.0.1",
        "port": 4321,
        "authkey": base64.b64encode(AUTHKEY).decode("ascii"),
        "instance_id": descriptor.instance_id,
        "debug": False,
    }
    assert "authkey" not in repr(descriptor)


def test_descriptor_is_only_removed_by_the_tunnel_that_published_it() -> None:
    first = TunnelDescriptor("test", port=1111, authkey=b"a" * 32)
    second = TunnelDescriptor("test", port=2222, authkey=b"b" * 32)
    first.publish()
    second.publish()

    first.remove()
    assert TunnelDescriptor.load("test") == second

    second.remove()
    assert not second.path.exists()


def test_a_failed_publish_leaves_no_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(descriptor_module.os, "replace", fail)

    with pytest.raises(OSError, match="disk full"):
        TunnelDescriptor("test", port=4321, authkey=AUTHKEY).publish()
    assert list(descriptor_path("test").parent.iterdir()) == []


def test_removing_an_absent_descriptor_is_a_no_op() -> None:
    TunnelDescriptor("test", port=4321, authkey=AUTHKEY).remove()


def write_descriptor(content: str) -> Path:
    path = descriptor_path("test")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def valid_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 2,
        "host": "127.0.0.1",
        "port": 4321,
        "authkey": base64.b64encode(AUTHKEY).decode("ascii"),
        "instance_id": "abc123",
        "debug": False,
    }
    return {**payload, **changes}


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '"text"',
        "{}",
        json.dumps(valid_payload(version=1)),
        json.dumps(valid_payload(host="0.0.0.0")),
        json.dumps(valid_payload(port=0)),
        json.dumps(valid_payload(port="4321")),
        json.dumps(valid_payload(authkey="not base64!")),
        json.dumps(valid_payload(authkey=base64.b64encode(b"short").decode())),
        json.dumps(valid_payload(instance_id=7)),
        json.dumps(valid_payload(debug="yes")),
        json.dumps({k: v for k, v in valid_payload().items() if k != "debug"}),
    ],
)
def test_an_unusable_descriptor_asks_to_restart_the_tunnel(content: str) -> None:
    """A descriptor from another version must fail loudly: restarting is trivial."""
    write_descriptor(content)

    with pytest.raises(IPCError, match="invalid; restart the tunnel"):
        TunnelDescriptor.load("test")


@pytest.mark.parametrize("content", ["not json", "[]", "{}"])
def test_remove_leaves_an_unusable_descriptor_alone(content: str) -> None:
    path = write_descriptor(content)

    TunnelDescriptor("test", port=4321, authkey=AUTHKEY).remove()

    assert path.read_text(encoding="utf-8") == content


def test_missing_descriptor_says_how_to_start_the_tunnel() -> None:
    with pytest.raises(
        IPCError, match="no tunnel is running for `missing`; start `ezhpcy tunnel`"
    ):
        TunnelDescriptor.load("missing")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_descriptor_is_owner_only_on_posix() -> None:
    descriptor = TunnelDescriptor("test", port=4321, authkey=AUTHKEY)
    descriptor.publish()

    assert descriptor.path.stat().st_mode & 0o077 == 0
    assert descriptor.path.parent.stat().st_mode & 0o077 == 0


# The descriptors live in a directory of their own inside the runtime directory,
# which `publish` creates or checks.
def descriptors_directory() -> Path:
    return descriptor_path("test").parent


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and symlink semantics")
def test_descriptor_rejects_symlinked_directory(tmp_path: Path) -> None:
    real_directory = tmp_path / "real-descriptors"
    real_directory.mkdir(mode=0o700)
    descriptors_directory().parent.mkdir(parents=True)
    descriptors_directory().symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(IPCError, match="must not be a symbolic link"):
        TunnelDescriptor("test", port=4321, authkey=AUTHKEY).publish()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file type semantics")
def test_descriptor_rejects_a_file_in_place_of_its_directory() -> None:
    descriptors_directory().parent.mkdir(parents=True)
    descriptors_directory().touch()

    with pytest.raises(IPCError, match="is not a directory"):
        TunnelDescriptor("test", port=4321, authkey=AUTHKEY).publish()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
def test_descriptor_rejects_directory_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptors_directory().mkdir(mode=0o700, parents=True)
    monkeypatch.setattr(
        descriptor_module.os,
        "getuid",
        lambda: descriptors_directory().stat().st_uid + 1,
    )

    with pytest.raises(IPCError, match="not owned by the current user"):
        TunnelDescriptor("test", port=4321, authkey=AUTHKEY).publish()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_descriptor_restricts_precreated_directory() -> None:
    descriptors_directory().mkdir(parents=True)
    descriptors_directory().chmod(0o777)

    TunnelDescriptor("test", port=4321, authkey=AUTHKEY).publish()

    assert descriptors_directory().stat().st_mode & 0o777 == 0o700
