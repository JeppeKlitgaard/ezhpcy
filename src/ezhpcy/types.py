import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ByteSize, Field, field_validator
from pydantic_extra_types.domain import DomainStr

from ezhpcy.constants import EZHPCY_VERSION, PACKAGE_NAME, PIXI_VERSION
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.utils import local_machine_id

_TIME_LIMIT_PATTERN = re.compile(r"^(?P<hours>\d+):(?P<minutes>\d{1,2})$")
PositiveByteSize = Annotated[ByteSize, Field(gt=0)]
PASSWORD_SOURCE_FIELDS = frozenset(
    {"password", "password_file", "password_fd", "password_keyring"}
)


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


class ProfileConfig(BaseModel):
    """An inheritable profile as written in the configuration file."""

    # EzHPCy configuration
    description: str | None = Field(default=None, min_length=1)
    inherit: str | None = Field(default=None, min_length=1)

    # Connection
    host: DomainStr | None = None
    user: str | None = Field(default=None, min_length=1)
    password: str | None = None
    password_file: Path | None = None
    password_fd: int | None = Field(default=None, ge=0)
    password_keyring: bool | None = None

    # Scheduler setup
    scheduler: SchedulerType | None = None
    queue: str | None = Field(default=None, min_length=1)
    cores: int | None = Field(default=None, ge=1)
    gpus: int | None = Field(default=None, ge=0)
    exclusive: bool | None = None
    time_limit: str | None = None
    memory: PositiveByteSize | None = None

    # Connection timings
    ssh_keepalive_interval_seconds: int | None = Field(default=None, gt=0)
    queue_timeout_seconds: float | None = Field(default=None, gt=0)
    worker_startup_timeout_seconds: float | None = Field(default=None, gt=0)

    # Scheduler options - Interactive
    interactive_submission_command: list[str] | None = Field(default=None, min_length=1)

    # Scheduler Options - LSF
    lsf_resource_reserve_per_task: bool | None = None
    lsf_application_profile: str | None = Field(default=None, min_length=1)
    lsf_submission_environment: dict[str, str] | None = None
    lsf_export_environment: list[str] | None = None

    # Scheduler Options - PBS
    pbs_command_directory: PurePosixPath | None = None

    @field_validator("time_limit")
    @classmethod
    def validate_time_limit(cls, value: str | None) -> str | None:
        parse_time_limit(value)
        return value


class _ResolvedConfigBase(BaseModel):
    """Fields shared by partially and fully resolved configurations."""

    # EzHPCy configuration
    description: str | None = None

    # Connection
    password: str | None = None
    password_file: Path | None = None
    password_fd: int | None = None
    password_keyring: bool = False

    # Scheduler setup
    scheduler: SchedulerType | None = None
    queue: str | None = None
    cores: int = Field(default=1, ge=1)
    gpus: int = Field(default=0, ge=0)
    exclusive: bool = False
    time_limit: str | None = None
    memory: PositiveByteSize | None = None

    # Connection timings
    ssh_keepalive_interval_seconds: int = Field(default=30, gt=0)
    queue_timeout_seconds: float = Field(default=15 * 60, gt=0)
    worker_startup_timeout_seconds: float = Field(default=60, gt=0)

    # Scheduler options - Interactive
    interactive_submission_command: list[str] | None = None

    ## Scheduler Options - LSF
    lsf_resource_reserve_per_task: bool = False
    lsf_application_profile: str | None = None
    lsf_submission_environment: dict[str, str] = Field(default_factory=dict)
    lsf_export_environment: list[str] = Field(default_factory=list)

    # Scheduler Options - PBS
    pbs_command_directory: PurePosixPath | None = None

    @field_validator("time_limit")
    @classmethod
    def validate_time_limit(cls, value: str | None) -> str | None:
        parse_time_limit(value)
        return value

    @property
    def time_limit_delta(self) -> timedelta | None:
        return parse_time_limit(self.time_limit)


class ResolvedProfileConfig(_ResolvedConfigBase):
    """An inherited profile before environment and CLI values are applied."""

    host: DomainStr | None = None
    user: str | None = None


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


class ResolvedConfig(_ResolvedConfigBase):
    """
    A profile after required environment and CLI values are applied.

    This enforces the invariants required for a full connection.
    """

    host: DomainStr
    user: str = Field(min_length=1)

    _DIGEST_EXCLUDE_FIELDS = frozenset(
        {
            # These fields are any that do not affect the identity of the connection or job setup.
            # Since these can have security implications, we conservatively _exclude_ them from the digest.
            # Note that only password and secret fields are excluded,
            # and environment variables should be included in the digest as they do affect the job setup.
            # Password fields are excluded.
            # In short, exclude anything that does not affect the identity of the connection or job setup
            # Ezhpcy configs
            "description",
            "inherit",
            # Connection timings
            "queue_timeout_seconds",
            "worker_startup_timeout_seconds",
            "ssh_keepalive_interval_seconds",
            # Password sources
            "password",
            "password_file",
            "password_fd",
            "password_keyring",
            #
        }
    )

    _DIGEST_INCLUDE_FIELDS = frozenset(
        {
            # These fields are only specified as a reminder that they are included in the digest.
            # This way, when new fields are added, we are forced to make a deliberate decision to include them!
            # Connection
            "host",
            "user",
            # Scheduler setup
            "scheduler",
            "queue",
            "cores",
            "gpus",
            "exclusive",
            "time_limit",
            "memory",
            # Scheduler options - Interactive
            "interactive_submission_command",
            # Scheduler options - LSF
            "lsf_resource_reserve_per_task",
            "lsf_application_profile",
            "lsf_submission_environment",
            "lsf_export_environment",
            # Scheduler options - PBS
            "pbs_command_directory",
        }
    )

    def descriptor_digest(self) -> str:
        """Return the identity for an anonymous descriptor."""
        # This is all a bit over the top
        digest_participants = {
            key: value
            for key, value in self.model_dump(
                exclude=self._DIGEST_EXCLUDE_FIELDS,
            ).items()
        }

        assert set(digest_participants.keys()) == self._DIGEST_INCLUDE_FIELDS, (
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
