import re
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ByteSize, Field, field_validator
from pydantic_extra_types.domain import DomainStr

from ezhpcy.constants import EZHPCY_VERSION, PACKAGE_NAME, PIXI_VERSION
from ezhpcy.scheduler.types import SchedulerType

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

    description: str | None = Field(default=None, min_length=1)
    inherit: str | None = Field(default=None, min_length=1)
    host: DomainStr | None = None
    user: str | None = Field(default=None, min_length=1)
    password: str | None = None
    password_file: Path | None = None
    password_fd: int | None = Field(default=None, ge=0)
    password_keyring: bool | None = None

    scheduler: SchedulerType | None = None
    queue: str | None = Field(default=None, min_length=1)
    cores: int | None = Field(default=None, ge=1)
    gpus: int | None = Field(default=None, ge=0)
    exclusive: bool | None = None
    time_limit: str | None = None
    memory: PositiveByteSize | None = None
    queue_timeout_seconds: float | None = Field(default=None, gt=0)
    worker_startup_timeout_seconds: float | None = Field(default=None, gt=0)
    interactive_submission_command: list[str] | None = Field(default=None, min_length=1)

    ## Scheduler Options
    # LSF Options
    lsf_resource_reserve_per_task: bool | None = None
    lsf_application_profile: str | None = Field(default=None, min_length=1)
    lsf_submission_environment: dict[str, str] | None = None
    lsf_export_environment: list[str] | None = None
    # PBS Options
    pbs_command_directory: PurePosixPath | None = None

    @field_validator("time_limit")
    @classmethod
    def validate_time_limit(cls, value: str | None) -> str | None:
        parse_time_limit(value)
        return value


class _ResolvedConfigBase(BaseModel):
    """Fields shared by partially and fully resolved configurations."""

    description: str | None = None
    password: str | None = None
    password_file: Path | None = None
    password_fd: int | None = None
    password_keyring: bool = False

    scheduler: SchedulerType | None = None
    queue: str | None = None
    cores: int = Field(default=1, ge=1)
    gpus: int = Field(default=0, ge=0)
    exclusive: bool = False
    time_limit: str | None = None
    memory: PositiveByteSize | None = None
    queue_timeout_seconds: float = Field(default=15 * 60, gt=0)
    worker_startup_timeout_seconds: float = Field(default=60, gt=0)
    interactive_submission_command: list[str] | None = None

    ## Scheduler Options
    # LSF Options
    lsf_resource_reserve_per_task: bool = False
    lsf_application_profile: str | None = None
    lsf_submission_environment: dict[str, str] = Field(default_factory=dict)
    lsf_export_environment: list[str] = Field(default_factory=list)
    # PBS Options
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
