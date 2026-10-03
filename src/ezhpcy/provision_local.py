import io
import os
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from ezhpcy.constants import (
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
    WORKER_HOST_KEY_NAME,
)
from ezhpcy.permissions import restrict_to_current_user


def ensure_local_key_pair(
    ssh_dir: Path, *, key_name: str, comment: str
) -> tuple[Path, Path]:
    """Create one dedicated local Ed25519 key pair if it does not exist."""
    private_key = ssh_dir / key_name
    public_key = private_key.with_suffix(".pub")

    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ssh_dir, 0o700)
    if private_key.exists() != public_key.exists():
        raise RuntimeError(
            f"Incomplete SSH key pair at {private_key}; restore or remove it."
        )
    if not private_key.exists():
        cryptography_key = ed25519.Ed25519PrivateKey.generate()
        private_key_text = cryptography_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        paramiko_key = paramiko.Ed25519Key.from_private_key(
            io.StringIO(private_key_text)
        )
        # Create with owner-only permissions immediately and refuse a raced-in file;
        # writing first and chmodding afterward briefly exposes private key material.
        private_key_fd = os.open(
            private_key,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(private_key_fd, "w", encoding="ascii", newline="\n") as file:
            file.write(private_key_text)
        public_key.write_text(
            f"{paramiko_key.get_name()} {paramiko_key.get_base64()} {comment}\n",
            encoding="ascii",
        )
    restrict_to_current_user(private_key)
    os.chmod(public_key, 0o644)
    return private_key, public_key


def ensure_local_ssh_keys(
    ssh_dir: Path,
) -> tuple[Path, Path, Path, Path]:
    """Ensure both worker client and worker host key pairs exist locally."""
    client_keys = ensure_local_key_pair(
        ssh_dir,
        key_name=WORKER_CLIENT_KEY_NAME,
        comment="ezhpcy worker client",
    )
    host_keys = ensure_local_key_pair(
        ssh_dir,
        key_name=WORKER_HOST_KEY_NAME,
        comment="ezhpcy worker host",
    )
    return *client_keys, *host_keys


def pin_worker_host_key(host_public_key: str, known_hosts: Path) -> None:
    """Replace EzHPCy's stable alias entry without touching unrelated hosts."""
    fields = host_public_key.strip().split()
    if len(fields) < 2:
        raise RuntimeError("The remote worker host public key is malformed.")
    replacement = f"{WORKER_HOST_ALIAS} {fields[0]} {fields[1]}\n"
    existing = known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
    retained = [
        line
        for line in existing.splitlines(keepends=True)
        if not line.split(maxsplit=1) or line.split(maxsplit=1)[0] != WORKER_HOST_ALIAS
    ]
    known_hosts.write_text("".join(retained) + replacement, encoding="utf-8")
    os.chmod(known_hosts, 0o600)
