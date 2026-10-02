import hashlib
import json
import re
from datetime import timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, ClassVar

from pydantic import BaseModel, ByteSize, ConfigDict, Field, field_validator
from pydantic_extra_types.domain import DomainStr

from ezhpcy.constants import EZHPCY_VERSION, PACKAGE_NAME, PIXI_VERSION
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.utils import local_machine_id

_TIME_LIMIT_PATTERN = re.compile(r"^(?P<hours>\d+):(?P<minutes>\d{1,2})$")
PositiveByteSize = Annotated[ByteSize, Field(gt=0)]
PASSWORD_SOURCE_FIELDS = frozenset(
    {"password", "password_file", "password_fd", "password_keyring"}
)


class SubmissionMode(StrEnum):
    """How the scheduler should start the worker allocation."""

    INTERACTIVE = "interactive"
    BATCH = "batch"


def parse_time_limit(value: str | None) -> timedelta | None:
    if value is None:
        return None
    match = _TIME_LIMIT_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("time_limit must use H:MM format")
    minutes = int(match.group("minutes"))
    if minutes >= 60:
        raise ValueError("time_limit minutes must be less than 60")
    result = timedelta(hours=int(match.group("hours")), minutes=minutes)
    if result <= timedelta(0):
        raise ValueError("time_limit must be positive")
    return result


class _SubConfig(BaseModel):
    """A group of related profile settings, written as one table per profile."""

    model_config = ConfigDict(extra="forbid")


class ConnectionConfig(_SubConfig):
    """How to reach and authenticate with the login node."""

    host: DomainStr | None = None
    user: str | None = Field(default=None, min_length=1)

    # Accepted only to reject it with a helpful message: passwords must not be
    # stored in configuration files.
    password: str | None = None
    password_file: Path | None = None
    password_fd: int | None = Field(default=None, ge=0)
    password_keyring: bool = False
    password_prompt: bool = True

    ssh_keepalive_interval_seconds: int = Field(default=30, gt=0)


class SchedulerConfig(_SubConfig):
    """Which scheduler allocates the worker, and how the job is submitted."""

    type: SchedulerType | None = None
    submission_mode: SubmissionMode | None = None
    interactive_submission_command: list[str] | None = Field(default=None, min_length=1)


class ResourcesConfig(_SubConfig):
    """What the worker job requests from the scheduler."""

    queue: str | None = Field(default=None, min_length=1)
    cores: int = Field(default=1, ge=1)
    gpus: int = Field(default=0, ge=0)
    exclusive: bool = False
    time_limit: str | None = None
    memory: PositiveByteSize | None = None

    @field_validator("time_limit")
    @classmethod
    def validate_time_limit(cls, value: str | None) -> str | None:
        parse_time_limit(value)
        return value

    @property
    def time_limit_delta(self) -> timedelta | None:
        return parse_time_limit(self.time_limit)

    @property
    def memory_bytes(self) -> int | None:
        return int(self.memory) if self.memory is not None else None


class TimingsConfig(_SubConfig):
    """Timeouts and intervals of the tunnel's scheduler and worker checks."""

    queue_timeout_seconds: float = Field(default=15 * 60, gt=0)
    worker_startup_timeout_seconds: float = Field(default=60, gt=0)
    job_poll_interval_seconds: float = Field(default=2.5, gt=0)
    job_monitor_interval_seconds: float = Field(default=60, gt=0)
    worker_heartbeat_interval_seconds: float = Field(default=30, gt=0)
    worker_heartbeat_timeout_seconds: float = Field(default=90, gt=0)


class LSFConfig(_SubConfig):
    """Options only used by the LSF scheduler."""

    resource_reserve_per_task: bool = False
    application_profile: str | None = Field(default=None, min_length=1)
    submission_environment: dict[str, str] = Field(default_factory=dict)
    export_environment: list[str] = Field(default_factory=list)


class PBSConfig(_SubConfig):
    """Options only used by the PBS scheduler."""

    command_directory: PurePosixPath | None = None


class ConnectionInfo(BaseModel):
    """A login-node connection with its password source resolved."""

    user: str | None = None
    password: str | None = None
    host: DomainStr
    password_prompt: bool = True
    ssh_keepalive_interval_seconds: int = Field(default=30, gt=0)


class _ProfileSettings(BaseModel):
    """The settings a profile holds, grouped into sub-configs."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, min_length=1)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    timings: TimingsConfig = Field(default_factory=TimingsConfig)
    lsf: LSFConfig = Field(default_factory=LSFConfig)
    pbs: PBSConfig = Field(default_factory=PBSConfig)


class ProfileConfig(_ProfileSettings):
    """An inheritable profile as written in the configuration file."""

    inherit: str | None = Field(default=None, min_length=1)


class ResolvedProfileConfig(_ProfileSettings):
    """
    An inherited profile before CLI values are applied.

    The `model_fields_set` of each sub-config holds the fields the profile or
    one of its ancestors configured.
    """


class RemoteState(BaseModel):
    """
    The state of the remote machine (login node) as discovered by EzHPCy.
    """

    cache_dir: PurePosixPath

    def package_cache_root(self) -> PurePosixPath:
        return self.cache_dir / PACKAGE_NAME

    def package_cache_dir(self) -> PurePosixPath:
        return self.package_cache_root() / EZHPCY_VERSION

    def pixi_home(self) -> PurePosixPath:
        return self.package_cache_dir() / "pixi" / PIXI_VERSION

    def pixi_executable(self) -> PurePosixPath:
        return self.pixi_home() / "bin" / "pixi"

    def pixi_cache_dir(self) -> PurePosixPath:
        return self.package_cache_dir() / "pixi_cache" / PIXI_VERSION

    def worker_cwd_dir(self) -> PurePosixPath:
        return self.package_cache_dir() / "worker_cwd"

    def worker_logs_dir(self) -> PurePosixPath:
        return self.package_cache_dir() / "logs" / "worker"


class ResolvedConfig(BaseModel):
    """
    A profile after CLI values are applied and its password source resolved.

    This enforces the invariants required for a full connection.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    connection: ConnectionInfo
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    timings: TimingsConfig = Field(default_factory=TimingsConfig)
    lsf: LSFConfig = Field(default_factory=LSFConfig)
    pbs: PBSConfig = Field(default_factory=PBSConfig)

    # Exclude anything that does not affect the identity of the connection or
    # job setup. Since these can have security implications, password fields
    # are conservatively excluded, while environment variables are included as
    # they do affect the job setup.
    _DIGEST_EXCLUDE_FIELDS: ClassVar[dict[str, bool | set[str]]] = {
        "description": True,
        "timings": True,
        "connection": {"password", "password_prompt", "ssh_keepalive_interval_seconds"},
    }

    # These are only listed as a reminder that they are included in the digest.
    # This way, when new fields are added, we are forced to make a deliberate
    # decision to include them!
    _DIGEST_INCLUDE_FIELDS: ClassVar[dict[str, frozenset[str]]] = {
        "connection": frozenset({"host", "user"}),
        "scheduler": frozenset(
            {"type", "submission_mode", "interactive_submission_command"}
        ),
        "resources": frozenset(
            {"queue", "cores", "gpus", "exclusive", "time_limit", "memory"}
        ),
        "lsf": frozenset(
            {
                "resource_reserve_per_task",
                "application_profile",
                "submission_environment",
                "export_environment",
            }
        ),
        "pbs": frozenset({"command_directory"}),
    }

    @field_validator("connection")
    @classmethod
    def validate_user(cls, value: ConnectionInfo) -> ConnectionInfo:
        if not value.user:
            raise ValueError("user must be set")
        return value

    def descriptor_digest(self) -> str:
        """Return the identity for an anonymous descriptor."""
        # This is all a bit over the top
        digest_participants = self.model_dump(
            mode="json", exclude=self._DIGEST_EXCLUDE_FIELDS
        )

        assert {
            section: frozenset(fields)
            for section, fields in digest_participants.items()
        } == self._DIGEST_INCLUDE_FIELDS, (
            "BUG: ResolvedConfig.digest_participants must include all fields in _DIGEST_INCLUDE_FIELDS "
            "and exclude all fields in _DIGEST_EXCLUDE_FIELDS"
        )

        # Canonicalize to ensure ordering, etc does not affect the digest.
        canonical_identity = json.dumps(
            digest_participants,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        # Compute a salt based on local machine identity
        salt_base = PACKAGE_NAME + local_machine_id()
        salt = hashlib.sha256(salt_base.encode("utf-8")).digest()[:16]

        # Use scrypt to make it computationally a bit more expensive to brute-force the digest
        digest = hashlib.scrypt(
            canonical_identity, salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return digest.hex()
