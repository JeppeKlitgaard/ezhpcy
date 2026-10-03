"""Secure publication and discovery of broker runtime descriptors."""

import base64
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from ezhpcy.ipc.common import (
    BrokerUnavailableError,
    IPCAddress,
    IPCError,
)

RUNTIME_DESCRIPTOR_VERSION = 2


@dataclass(frozen=True)
class RuntimeDescriptor:
    address: IPCAddress
    authkey: bytes
    instance_id: str
    debug: bool


def load_runtime_descriptor(path: Path) -> RuntimeDescriptor:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BrokerUnavailableError(
            "foreground broker is not running; start `ezhpcy broker`"
        ) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error

    try:
        version = payload["version"]
        host = payload["host"]
        port = payload["port"]
        encoded_authkey = payload["authkey"]
        instance_id = payload["instance_id"]
        debug = payload["debug"]
        if version != RUNTIME_DESCRIPTOR_VERSION:
            raise ValueError("unsupported runtime descriptor version")
        if not all(
            isinstance(value, str) for value in (host, encoded_authkey, instance_id)
        ):
            raise ValueError("runtime descriptor values have invalid types")
        if not isinstance(debug, bool):
            raise ValueError("runtime descriptor values have invalid types")
        address = IPCAddress(host, port)
        authkey = base64.b64decode(encoded_authkey, validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise BrokerUnavailableError(
            "broker runtime information is invalid; restart the foreground broker"
        ) from error

    return RuntimeDescriptor(address, authkey, instance_id, debug)


def publish_runtime_descriptor(
    path: Path,
    *,
    address: IPCAddress,
    authkey: bytes,
    instance_id: str,
    debug: bool,
) -> None:
    payload = {
        "version": RUNTIME_DESCRIPTOR_VERSION,
        "host": address.host,
        "port": address.port,
        "authkey": base64.b64encode(authkey).decode("ascii"),
        "instance_id": instance_id,
        "debug": debug,
    }
    directory = path.parent
    directory_fd = _prepare_runtime_directory(directory)
    temporary_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary = directory / temporary_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        if directory_fd is None:
            descriptor = os.open(temporary, flags, 0o600)
        else:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        if directory_fd is None:
            os.replace(temporary, path)
        else:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
    finally:
        try:
            try:
                if directory_fd is None:
                    temporary.unlink()
                else:
                    os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


def _prepare_runtime_directory(directory: Path) -> int | None:
    """Create a private runtime directory and pin its audited POSIX inode."""
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    except OSError as error:
        raise IPCError("could not create the broker runtime directory") from error

    if os.name != "posix":
        # Python 3.14 applies an owner-and-administrators-only ACL when mode 0700
        # creates a directory on Windows. Files then inherit that protected ACL.
        return None

    try:
        metadata = directory.lstat()
    except OSError as error:
        raise IPCError("could not inspect the broker runtime directory") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise IPCError("broker runtime directory must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise IPCError("broker runtime path is not a directory")
    if metadata.st_uid != os.getuid():
        raise IPCError("broker runtime directory is not owned by the current user")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except OSError as error:
        raise IPCError(
            "could not securely open the broker runtime directory"
        ) from error

    try:
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise IPCError("broker runtime path is not a directory")
        if metadata.st_uid != os.getuid():
            raise IPCError("broker runtime directory is not owned by the current user")
        try:
            os.fchmod(directory_fd, 0o700)
        except OSError as error:
            raise IPCError(
                "could not restrict the broker runtime directory permissions"
            ) from error
        if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
            raise IPCError("broker runtime directory permissions are not private")
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def remove_runtime_descriptor(path: Path, *, instance_id: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("instance_id") == instance_id:
            path.unlink()
    except FileNotFoundError, OSError, json.JSONDecodeError:
        pass
