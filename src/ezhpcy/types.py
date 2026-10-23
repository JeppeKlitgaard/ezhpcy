import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, TypedDict
lazy import hashlib
lazy import ipaddress
lazy import json
lazy from collections.abc import Mapping
lazy from pathlib import Path, PurePosixPath
lazy from string.templatelib import Template
lazy from typing import Self, Unpack

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ByteSize,
    ConfigDict,
    Field,
    PlainSerializer,
    Strict,
    ValidationError,
    field_validator,
    model_validator,
)
lazy from pydantic_extra_types.domain import DomainStr

from ezhpcy.messages import MessageError
lazy from ezhpcy.constants import (
    APPS_DIRECTORY_NAME,
    EZHPCY_VERSION,
    PACKAGE_NAME,
    PIXI_VERSION,
)
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.utils import local_machine_id

_TIME_LIMIT_PATTERN = re.compile(r"^(?P<hours>\d+):(?P<minutes>\d{1,2})$")
PositiveByteSize = Annotated[ByteSize, Field(gt=0)]


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
_ZERO = timedelta(0)


def _as_seconds(value: timedelta) -> int | float:
    seconds = value.total_seconds()
    return int(seconds) if seconds.is_integer() else seconds


def _whole_seconds(value: timedelta) -> timedelta:
    if value % timedelta(seconds=1):
        raise ValueError("must be a whole number of seconds")
    return value


# A duration, written in the configuration file (and dumped to JSON) in seconds.
Seconds = Annotated[timedelta, PlainSerializer(_as_seconds, when_used="json")]


class _SubConfig(BaseModel):
    """
    A group of related profile settings, written as one table per profile. Frozen:
    a job's configuration is a value (see `JobConfig`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class PasswordConfig(_SubConfig):
    """How to get the login-node password.

    The sources are tried in this order, skipping those not set or disabled:
    `password_fd`, `password_file`, `password_keyring`, then `password_prompt`.
    """

    # Accepted only to reject it with a helpful message: passwords must not be
    # stored in configuration files.
    password: str | None = None
    # `false` disables an inherited file or descriptor: TOML has no null.
    password_fd: Annotated[int, Strict(), Field(ge=0)] | Literal[False] | None = None
    password_file: Path | Literal[False] | None = None
    password_keyring: bool = False
    password_prompt: bool = True


class ConnectionConfig(PasswordConfig):
    """How to reach and authenticate with the login node."""

    host: DomainStr | None = None
    user: str | None = Field(default=None, min_length=1)

    ssh_keepalive_interval_seconds: int = Field(default=30, gt=0)


class _SchedulerBase(_SubConfig):
    """The scheduler settings a profile may leave out even for a tunnel."""

    interactive_submission_command: tuple[str, ...] | None = Field(
        default=None, min_length=1
    )

    @field_validator("interactive_submission_command")
    @classmethod
    def validate_interactive_submission_command(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is not None and any(
            not argument or "\0" in argument for argument in value
        ):
            raise ValueError("arguments must not be empty or contain NUL")
        return value


class SchedulerConfig(_SchedulerBase):
    """Which scheduler allocates the worker, and how the job is submitted."""

    type: SchedulerType | None = None
    submission_mode: SubmissionMode | None = None


class SchedulerSettings(_SchedulerBase):
    """
    Which scheduler allocates the worker, and how the job is submitted: a
    `SchedulerConfig` with every setting a job needs.

    `interactive_submission_command` is a site's wrapper (e.g. DTU's `a100sh`),
    run instead of the scheduler's own request; see `JobConfig`.
    """

    type: SchedulerType
    submission_mode: SubmissionMode


class Resources(_SubConfig):
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


class Timings(_SubConfig):
    """
    Timeouts and intervals of the job's scheduler and worker checks. Each is a
    `timedelta`, written in the configuration file in seconds, as its name says.
    """

    # How long the job may queue.
    queue_timeout_seconds: Seconds = Field(default=timedelta(minutes=15), gt=_ZERO)
    # From the job's start, how long the worker's sshd may take to answer.
    worker_startup_timeout_seconds: Seconds = Field(
        default=timedelta(minutes=2), gt=_ZERO
    )
    # How often the scheduler and the worker are checked while the job starts.
    job_poll_interval_seconds: Seconds = Field(default=timedelta(seconds=2.5), gt=_ZERO)
    # How often the scheduler is asked whether the job still runs, once it is ready.
    job_monitor_interval_seconds: Seconds = Field(
        default=timedelta(minutes=1), gt=_ZERO
    )
    # How often the worker's lease is renewed, and how long the worker may go
    # without a renewal before it cancels its own job (whole seconds).
    worker_heartbeat_interval_seconds: Seconds = Field(
        default=timedelta(seconds=30), gt=_ZERO
    )
    worker_heartbeat_timeout_seconds: Annotated[
        Seconds, AfterValidator(_whole_seconds)
    ] = Field(default=timedelta(seconds=90), gt=_ZERO)


class LSFOptions(_SubConfig):
    """Options only used by the LSF scheduler."""

    resource_reserve_per_task: bool = False
    application_profile: str | None = Field(default=None, min_length=1)
    submission_environment: dict[str, str] = Field(default_factory=dict)
    export_environment: tuple[str, ...] = ()


class PBSOptions(_SubConfig):
    """Options only used by the PBS scheduler."""

    command_directory: PurePosixPath | None = None


class AppsConfig(_SubConfig):
    """Where apps keep their environments, assets and workdirs on their host."""

    # Default: `ezhpcy-apps` under the host's XDG cache directory. Environments
    # run to gigabytes, so a site may want them off a small home.
    root: PurePosixPath | None = None

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: PurePosixPath | None) -> PurePosixPath | None:
        if value is not None and not value.is_absolute():
            raise ValueError("root must be an absolute path on the host")
        return value


class _ProfileSettings(BaseModel):
    """The settings a profile holds, grouped into sub-configs."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, min_length=1)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    resources: Resources = Field(default_factory=Resources)
    timings: Timings = Field(default_factory=Timings)
    lsf: LSFOptions = Field(default_factory=LSFOptions)
    pbs: PBSOptions = Field(default_factory=PBSOptions)
    apps: AppsConfig = Field(default_factory=AppsConfig)


class ProfileConfig(_ProfileSettings):
    """An inheritable profile as written in the configuration file."""

    inherit: str | None = Field(default=None, min_length=1)


class ResolvedProfileConfig(_ProfileSettings):
    """
    An inherited profile before CLI values are applied.

    The `model_fields_set` of each sub-config holds the fields the profile, one
    of its ancestors, or the top-level `connection` table configured.
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

    def pixi_command(
        self, *args: str, cache_dir: PurePosixPath | None = None
    ) -> tuple[str, ...]:
        """
        Run the pinned private Pixi with `cache_dir` as its cache, by default
        EzHPCy's XDG-resolved one.
        """
        return (
            "env",
            f"PIXI_HOME={self.pixi_home()}",
            f"PIXI_CACHE_DIR={cache_dir or self.pixi_cache_dir()}",
            str(self.pixi_executable()),
            *args,
        )

    def apps_root(self, configured: PurePosixPath | None = None) -> PurePosixPath:
        """
        Where app environments, assets and workdirs live: `configured` (the
        profile's `apps.root`), or by default a sibling of the package cache root,
        so that pruning stale versions never sees it.
        """
        return configured or self.cache_dir / APPS_DIRECTORY_NAME

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


def _ip_address(value: str) -> str:
    ipaddress.ip_address(value)
    return value


class Login(BaseModel):
    """
    A login node to connect to, and where its password comes from: never the
    password itself, which is read only when connecting (see `ezhpcy.login`).

    The password's sources are tried in this order, skipping those not set:
    `password_fd`, `password_file`, the keyring (`password_keyring`), and last,
    if public-key authentication fails, asking for it (`password_prompt`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # A domain name, or an IP address.
    host: DomainStr | Annotated[str, AfterValidator(_ip_address)]
    user: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    # How often to check that the connection is alive.
    keepalive_interval: timedelta = Field(default=timedelta(seconds=30), gt=_ZERO)
    password_fd: Annotated[int, Strict(), Field(ge=0)] | None = None
    password_file: Path | None = None
    password_keyring: bool = False
    password_prompt: bool = True

    # Which fields name an anonymous tunnel (see `alias_digest`): the excluded
    # ones, and as a reminder, the included ones. The password's sources are
    # excluded conservatively. tests/human_reviewed/test_alias_digest.py checks
    # that every field is in one or the other, so that a new field forces a
    # deliberate decision.
    _DIGEST_EXCLUDE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "port",
            "keepalive_interval",
            "password_fd",
            "password_file",
            "password_keyring",
            "password_prompt",
        }
    )
    _DIGEST_INCLUDE_FIELDS: ClassVar[frozenset[str]] = frozenset({"host", "user"})


class ResourceChanges(TypedDict, total=False):
    queue: str | None
    cores: int
    gpus: int
    exclusive: bool
    time_limit: timedelta | None
    memory: int | None


class TimingChanges(TypedDict, total=False):
    queue_timeout_seconds: timedelta
    worker_startup_timeout_seconds: timedelta
    job_poll_interval_seconds: timedelta
    job_monitor_interval_seconds: timedelta
    worker_heartbeat_interval_seconds: timedelta
    worker_heartbeat_timeout_seconds: timedelta


class SchedulerChanges(TypedDict, total=False):
    type: SchedulerType
    submission_mode: SubmissionMode
    interactive_submission_command: tuple[str, ...] | None


class JobConfig(BaseModel):
    """
    A worker job: its scheduler, what it requests, and its timings.

    An `interactive_submission_command` replaces the scheduler's own request, so
    it needs the interactive submission mode, and decides the resources (and for
    LSF, the `lsf` options) itself: setting any of those with it is an error,
    rather than silently getting something else. Setting means giving a value,
    even the default (`model_fields_set`).

    With `auto_provision`, the worker infrastructure is provisioned or
    repaired before the job is submitted; without, it is only checked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheduler: SchedulerSettings
    resources: Resources = Field(default_factory=Resources)
    timings: Timings = Field(default_factory=Timings)
    lsf: LSFOptions = Field(default_factory=LSFOptions)
    pbs: PBSOptions = Field(default_factory=PBSOptions)
    auto_provision: bool = True

    # As `Login`'s: which fields name an anonymous tunnel.
    _DIGEST_EXCLUDE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"timings", "auto_provision"}
    )
    _DIGEST_INCLUDE_FIELDS: ClassVar[dict[str, frozenset[str]]] = {
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

    @model_validator(mode="after")
    def check_interactive_submission_command(self) -> Self:
        scheduler = self.scheduler
        if scheduler.interactive_submission_command is None:
            return self
        if scheduler.submission_mode is not SubmissionMode.INTERACTIVE:
            raise SubmissionCommandError(
                t"{'scheduler.interactive_submission_command':name} requires "
                t"{'scheduler.submission_mode':name} to be {'interactive':choice}",
                conflicts=(),
            )
        conflicts = tuple(
            f"{section}.{name}"
            for section in submission_sections(scheduler.type)
            for name in sorted(getattr(self, section).model_fields_set)
        )
        if conflicts:
            raise SubmissionCommandError(
                t"{'scheduler.interactive_submission_command':name} replaces the "
                t"scheduler-generated request and cannot be combined with "
                t"submission options: {conflicts:name}",
                conflicts=conflicts,
            )
        return self

    def with_resources(self, **changes: Unpack[ResourceChanges]) -> Self:
        """A copy with `changes` to `resources`; raises `ConfigError` if invalid."""
        return self._with("resources", changes)

    def with_timings(self, **changes: Unpack[TimingChanges]) -> Self:
        """A copy with `changes` to `timings`; raises `ConfigError` if invalid."""
        return self._with("timings", changes)

    def with_scheduler(self, **changes: Unpack[SchedulerChanges]) -> Self:
        """A copy with `changes` to `scheduler`; raises `ConfigError` if invalid."""
        return self._with("scheduler", changes)

    def _with(self, section: str, changes: Mapping[str, object]) -> Self:
        # What was set stays set, so that the checks above still see it.
        values = self.model_dump(exclude_unset=True)
        values[section] = {**values.get(section, {}), **changes}
        return validated(type(self), values)


def submission_sections(scheduler: SchedulerType) -> tuple[str, ...]:
    """The `JobConfig` sections that an interactive submission command decides."""
    return ("resources", "lsf") if scheduler is SchedulerType.LSF else ("resources",)


def alias_digest(login: Login, job: JobConfig) -> str:
    """Return the identity that names an anonymous tunnel's alias."""
    digest_participants = {
        "connection": login.model_dump(
            mode="json", exclude=set(Login._DIGEST_EXCLUDE_FIELDS)
        ),
        **job.model_dump(mode="json", exclude=set(JobConfig._DIGEST_EXCLUDE_FIELDS)),
    }

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
    digest = hashlib.scrypt(canonical_identity, salt=salt, n=2**14, r=8, p=1, dklen=32)
    return digest.hex()


class ConfigError(MessageError, ValueError):
    """
    A configuration that can't be used. `setting` is the one at fault, if one
    is, by its path in the model it configures (`user` in a `Login`,
    `resources.cores` in a `JobConfig`), `value` what it was, and `detail` what
    is wrong with it, without naming it.
    """

    def __init__(
        self,
        message: Template,
        *,
        setting: str | None = None,
        value: object = None,
        detail: Template | None = None,
    ) -> None:
        super().__init__(message)
        self.setting = setting
        self.value = value
        self.detail = message if detail is None else detail

    @classmethod
    def of_setting(
        cls, setting: str, detail: Template, *, value: object = None
    ) -> ConfigError:
        """An error in `setting`: `detail` says what."""
        return cls(
            t"{setting:name}: {detail}", setting=setting, value=value, detail=detail
        )


class MissingSetting(ConfigError):
    """
    `setting` must be set, and isn't. `key` is where it would be set in the
    configuration file (`profile.<name>.connection.user`).
    """

    def __init__(self, setting: str, *, key: str) -> None:
        super().__init__(t"{key:name} must be set", setting=setting)
        self.key = key


class SubmissionCommandError(ConfigError):
    """
    An interactive submission command that the job's other settings contradict:
    the submission mode, or `conflicts`, which the command would ignore.
    """

    def __init__(self, message: Template, *, conflicts: tuple[str, ...]) -> None:
        super().__init__(message, setting="scheduler.interactive_submission_command")
        self.conflicts = conflicts


def validated[ModelT: BaseModel](model: type[ModelT], values: object) -> ModelT:
    """`model` validated from `values`; raises `ConfigError` for the first problem."""
    try:
        return model.model_validate(values)
    except ValidationError as error:
        raise config_error(error) from error


def config_error(error: ValidationError) -> ConfigError:
    """The first of Pydantic's errors, as a `ConfigError`."""
    details = error.errors(include_url=False)[0]
    cause = details.get("ctx", {}).get("error")
    if isinstance(cause, ConfigError):
        return cause
    # Pydantic prefixes a validator's own message with "Value error, ".
    message = str(cause) if details["type"] == "value_error" else details["msg"]
    if not details["loc"]:
        return ConfigError(t"{message}")
    setting = ".".join(map(str, details["loc"]))
    return ConfigError.of_setting(setting, t"{message}", value=details["input"])
