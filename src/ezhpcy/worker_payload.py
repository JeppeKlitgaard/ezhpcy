import hashlib
import logging
import secrets
import shlex
from importlib import resources
from pathlib import PurePosixPath

from jinja2 import StrictUndefined, Template

from ezhpcy.constants import EZHPCY_VERSION, OPENSSH_MATCHSPEC, PIXI_VERSION
from ezhpcy.ssh import SSHClient
from ezhpcy.types import RemoteState

SSH_SERVE_RESOURCE = "static/data/ssh-serve.sh.j2"
PAYLOAD_DIRECTORY_NAME = "payloads"
logger = logging.getLogger(__name__)


class WorkerPayloadError(RuntimeError):
    pass


def render_worker_payload() -> bytes:
    """Render the worker script to the exact bytes stored on the remote host."""
    template = resources.files("ezhpcy").joinpath(SSH_SERVE_RESOURCE)
    rendered = Template(
        template.read_text(encoding="utf-8"), undefined=StrictUndefined
    ).render(
        openssh_matchspec=shlex.quote(OPENSSH_MATCHSPEC),
        ezhpcy_version=EZHPCY_VERSION,
        pixi_version=shlex.quote(PIXI_VERSION),
    )
    normalized = rendered.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized.encode("utf-8")


def worker_payload_path(
    remote_state: RemoteState, payload: bytes | None = None
) -> PurePosixPath:
    """Return the absolute content-addressed path for a rendered payload."""
    payload = payload if payload is not None else render_worker_payload()
    digest = hashlib.sha256(payload).hexdigest()
    return (
        remote_state.package_cache_dir() / PAYLOAD_DIRECTORY_NAME / digest / "ssh-serve"
    )


def require_worker_payload(ssh: SSHClient, remote_state: RemoteState) -> PurePosixPath:
    """Return the current payload path only when its remote bytes are exact."""
    payload = render_worker_payload()
    remote_path = worker_payload_path(remote_state, payload)
    try:
        with ssh.sftp_client() as sftp:
            existing = sftp.read_bytes(remote_path)
    except FileNotFoundError:
        raise WorkerPayloadError(
            "the current worker payload is missing; run `ezhpcy tunnel provision`"
        ) from None
    if existing != payload:
        raise WorkerPayloadError(
            "the current worker payload is corrupted; run `ezhpcy tunnel provision`"
        )
    return remote_path


def ensure_worker_payload(ssh: SSHClient, remote_state: RemoteState) -> PurePosixPath:
    """Upload the current worker payload atomically unless it already matches."""
    payload = render_worker_payload()
    remote_path = worker_payload_path(remote_state, payload)
    remote_directory = remote_path.parent

    with ssh.sftp_client() as sftp:
        sftp.mkdir(remote_directory, mode=0o700, parents=True, exist_ok=True)
        sftp.chmod(str(remote_directory), 0o700)
        try:
            existing = sftp.read_bytes(remote_path)
        except FileNotFoundError:
            existing = None

        if existing == payload:
            sftp.chmod(str(remote_path), 0o755)
            logger.debug("Worker payload is current at %s", remote_path)
            return remote_path

        if existing is None:
            logger.info("Provisioning worker payload at %s", remote_path)
        else:
            logger.warning("Replacing corrupted worker payload at %s", remote_path)

        temporary_path = remote_directory / (
            f".{remote_path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            sftp.write_bytes(temporary_path, payload)
            sftp.chmod(str(temporary_path), 0o755)
            sftp.posix_rename(str(temporary_path), str(remote_path))
        except BaseException:
            try:
                sftp.remove(str(temporary_path))
            except OSError:
                pass
            raise

        logger.info("Worker payload ready at %s", remote_path)

    return remote_path
