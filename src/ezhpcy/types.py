import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, ClassVar
lazy import hashlib
lazy import json
lazy from pathlib import Path, PurePosixPath

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ByteSize,
    ConfigDict,
    Field,
    PlainSerializer,
    Strict,
    field_validator,
)
lazy from pydantic_extra_types.domain import DomainStr

lazy from ezhpcy.constants import EZHPCY_VERSION, PACKAGE_NAME, PIXI_VERSION
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.utils import local_machine_id

_TIME_LIMIT_PATTERN = re.compile(r"^(?P<hours>\d+):(?P<minutes>\d{1,2})$")
PositiveByteSize = Annotated[ByteSize, Field(gt=0)]
PASSWORD_SOURCE_FIELDS = frozenset(
    {"password", "password_file", "password_fd", "password_keyring"}
)


class SubmissionMode(StrEnum):
    """How the scheduler should start the worker allocation."""

    INTERACTIVE = "interactive"
    BATCH = "batch"


def _parse_time_limit(value: object) -> object:
    """Parse `H:MM`, and leave anything else, such as the `timedelta` of a Python
    dump, to the field's own validation."""
    if not isinstance(value, str):
        return value
    match = _TIME_LIMIT_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("time_limit must use H:MM format")
    minutes = int(match.group("minutes"))
    if minutes >= 60:
        raise ValueError("time_limit minutes must be less than 60")
    return timedelta(hours=int(match.group("hours")), minutes=minutes)


def _check_time_limit(value: timedelta) -> timedelta:
    if value <= timedelta(0):
        raise ValueError("time_limit must be positive")
    # `H:MM` has no seconds to write back.
    if value % timedelta(minutes=1):
        raise ValueError("time_limit must be a whole number of minutes")
    return value


def _format_time_limit(value: timedelta) -> str:
    hours, minutes = divmod(value // timedelta(minutes=1), 60)
    return f"{hours}:{minutes:02d}"


# Written and dumped to JSON (as the digest is) as `H:MM`.
TimeLimit = Annotated[
    timedelta,
    BeforeValidator(_parse_time_limit),
    Strict(),
    AfterValidator(_check_time_limit),
    PlainSerializer(_format_time_limit, when_used="json"),
]


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


class _SchedulerSettings(_SubConfig):
    """The scheduler settings a profile may leave out even for a tunnel."""

    interactive_submission_command: list[str] | None = Field(default=None, min_length=1)

    @field_validator("interactive_submission_command")
    @classmethod
    def validate_interactive_submission_command(
        cls, value: list[str] | None
    ) -> list[str] | None:
        if value is not None and any(
            not argument or "\0" in argument for argument in value
        ):
            raise ValueError("arguments must not be empty or contain NUL")
        return value


class SchedulerConfig(_SchedulerSettings):
    """Which scheduler allocates the worker, and how the job is submitted."""

    type: SchedulerType | None = None
    submission_mode: SubmissionMode | None = None


class ResolvedSchedulerConfig(_SchedulerSettings):
    """A scheduler config with the settings a tunnel needs set."""

    type: SchedulerType
    submission_mode: SubmissionMode


class ResourcesConfig(_SubConfig):
    """What the worker job requests from the scheduler."""

    queue: str | None = Field(default=None, min_length=1)
    cores: int = Field(default=1, ge=1)
    gpus: int = Field(default=0, ge=0)
    exclusive: bool = False
    time_limit: TimeLimit | None = None
    memory: PositiveByteSize | None = None

    @property
    def memory_bytes(self) -> int | None:
        return int(self.memory) if self.memory is not None else None


class TimingsConfig(_SubConfig):
    """Timeouts and intervals of the tunnel's scheduler and worker checks."""

    queue_timeout_seconds: float = Field(default=15 * 60, gt=0)
    worker_startup_timeout_seconds: float = Field(default=120, gt=0)
    job_poll_interval_seconds: float = Field(default=2.5, gt=0)
    job_monitor_interval_seconds: float = Field(default=60, gt=0)
    worker_heartbeat_interval_seconds: float = Field(default=30, gt=0)
    worker_heartbeat_timeout_seconds: int = Field(default=90, gt=0)


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

    user: str = Field(min_length=1)
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
    state_dir: PurePosixPath

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

    def pixi_command(self, *args: str) -> tuple[str, ...]:
        """Run the pinned private Pixi with EzHPCy's XDG-resolved cache."""
        return (
            "env",
            f"PIXI_HOME={self.pixi_home()}",
            f"PIXI_CACHE_DIR={self.pixi_cache_dir()}",
            str(self.pixi_executable()),
            *args,
        )

    def worker_cwd_dir(self) -> PurePosixPath:
        return self.package_cache_dir() / "worker_cwd"

    def package_state_root(self) -> PurePosixPath:
        return self.state_dir / PACKAGE_NAME

    def package_state_dir(self) -> PurePosixPath:
        return self.package_state_root() / EZHPCY_VERSION

    def worker_logs_dir(self) -> PurePosixPath:
        return self.package_state_dir() / "logs" / "worker"

    def worker_control_root(self) -> PurePosixPath:
        return self.package_state_dir() / "worker_control"


class ResolvedConfig(BaseModel):
    """
    A profile after CLI values are applied and its password source resolved.

    This enforces the invariants required for a full connection.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    connection: ConnectionInfo
    scheduler: ResolvedSchedulerConfig
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
    # tests/human_reviewed/test_descriptor_digest.py checks every field is either
    # here or excluded above, so a new field forces a deliberate decision to
    # include it!
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

    def descriptor_digest(self) -> str:
        """Return the identity for an anonymous descriptor."""
        digest_participants = self.model_dump(
            mode="json", exclude=self._DIGEST_EXCLUDE_FIELDS
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
