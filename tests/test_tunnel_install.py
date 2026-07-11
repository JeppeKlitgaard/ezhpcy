from pathlib import Path, PurePosixPath

from ezhpcy.cli.tunnel.install import (
    WORKER_HOST_ALIAS,
    _pin_worker_host_key,
    _render_sshd_config,
)


def test_render_sshd_config_replaces_remote_values() -> None:
    rendered = _render_sshd_config(
        "AllowUsers {{ remote_username }}\nHostKey {{ remote_config_dir }}/host\n",
        remote_username="alice",
        remote_config_dir=PurePosixPath("/home/alice/.config/ezhpcy/ssh"),
    )

    assert rendered == ("AllowUsers alice\nHostKey /home/alice/.config/ezhpcy/ssh/host")


def test_pin_worker_host_key_preserves_unrelated_entries(tmp_path: Path) -> None:
    known_hosts = tmp_path / "worker_known_hosts"
    known_hosts.write_text(
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 OLD\n",
        encoding="utf-8",
    )

    _pin_worker_host_key("ssh-ed25519 NEW remote-comment\n", known_hosts)

    assert known_hosts.read_text(encoding="utf-8") == (
        f"other ssh-ed25519 OTHER\n{WORKER_HOST_ALIAS} ssh-ed25519 NEW\n"
    )
