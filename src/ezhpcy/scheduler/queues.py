"""What a cluster's queues accept, as its scheduler describes them to one user."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
lazy from collections.abc import Callable, Sequence
lazy from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from ezhpcy.scheduler.types import SchedulerType


class Unavailable(StrEnum):
    """Why a queue won't run the user's jobs."""

    NOT_PERMITTED = "not_permitted"
    CLOSED = "closed"
    # It takes jobs, but doesn't start them.
    STOPPED = "stopped"
    BLOCKED_BY_LIMIT = "blocked_by_limit"
    # A routing queue none of whose destinations take the user's jobs.
    NO_ROUTE = "no_route"


class LimitSource(StrEnum):
    """Where a queue's limits come from."""

    QUEUE = "queue"
    SERVER = "server"
    # Limits on what one user's jobs take together.
    USER = "user"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(_Model):
    """
    What one source of limits allows a job, `None` being no limit. `jobs` is
    how many jobs a user may have at once.
    """

    time_limit: timedelta | None = None
    default_time_limit: timedelta | None = None
    cpu_time: timedelta | None = None
    memory_bytes: int | None = None
    cores: int | None = None
    gpus: int | None = None
    jobs: int | None = None

    def tighten(self, other: Limits) -> Limits:
        """Both limits at once: the smaller of each, and `self`'s default first."""
        return Limits(
            time_limit=_smallest(self.time_limit, other.time_limit),
            default_time_limit=self.default_time_limit or other.default_time_limit,
            cpu_time=_smallest(self.cpu_time, other.cpu_time),
            memory_bytes=_smallest(self.memory_bytes, other.memory_bytes),
            cores=_smallest(self.cores, other.cores),
            gpus=_smallest(self.gpus, other.gpus),
            jobs=_smallest(self.jobs, other.jobs),
        )


class ExecutionHost(_Model):
    """An execution host, as the scheduler sees it now."""

    name: str
    # Whether the scheduler would start jobs on it, were it free.
    usable: bool
    slots: int | None = None
    cores: int | None = None
    memory_bytes: int | None = None
    free_memory_bytes: int | None = None
    gpus: int = 0
    gpu_model: str | None = None

    @property
    def has_gpus(self) -> bool:
        return self.gpus > 0


type GPUMode = Literal["shared", "exclusive_process"]


class Submission(_Model):
    """One way into a queue: the scheduler's own command, or a site's wrapper."""

    interactive: bool
    # A site's wrapper; `None` for the scheduler's own `bsub` or `qsub`.
    command: PurePosixPath | None = None
    # Whether the login shell finds the command by its name, rather than another
    # of that name or none.
    on_path: bool = True
    # The routing queue the job goes through.
    via: str | None = None
    # The LSF application profile the job runs under.
    profile: str | None = None
    limits: Limits = Field(default_factory=Limits)
    # Whether the job's GPUs are its own (`exclusive_process`) or shared with
    # other jobs', as LSF's `-gpu` mode; `None` where the queue's hosts have no
    # GPUs, or the scheduler has no such mode (PBS).
    gpu_mode: GPUMode | None = None


class Queue(_Model):
    """A queue, and the ways the user may submit to it."""

    name: str
    description: str | None = None
    unavailable: Unavailable | None = None
    # Where a routing queue sends the user's jobs.
    routes_to: str | None = None
    hosts: tuple[str, ...] = ()
    # Hosts the queue only uses once its own are full, also in `hosts`.
    overflow_hosts: tuple[str, ...] = ()
    # Whether a job may ask for its hosts to itself (`bsub -x`, PBS's
    # `place=excl`); a routing queue's is that of where it sends the user's jobs.
    exclusive_hosts: bool = False
    limits: dict[LimitSource, Limits] = Field(default_factory=dict)
    # Empty when the user can't submit to the queue, or only through a wrapper
    # EzHPCy wasn't told where to find.
    submissions: tuple[Submission, ...] = ()

    @property
    def available(self) -> bool:
        return self.unavailable is None


class Cluster(_Model):
    """A cluster's hosts and queues, as one user sees them at `generated_at`."""

    scheduler: SchedulerType
    name: str | None = None
    version: str | None = None
    user: str
    generated_at: datetime
    default_queue: str | None = None
    hosts: dict[str, ExecutionHost] = Field(default_factory=dict)
    queues: tuple[Queue, ...] = ()

    def queue(self, name: str) -> Queue:
        """The queue called `name`. Raises `KeyError` if there is none."""
        for queue in self.queues:
            if queue.name == name:
                return queue
        raise KeyError(name)

    def request_limits(self, queue: Queue, submission: Submission) -> Limits:
        """
        The most a job submitted to `queue` through `submission` may ask for:
        its limits, those of the queues it goes through, and the largest usable
        host of the queue it ends up in. A queue with no usable hosts allows no
        cores, memory or GPUs.
        """
        queues = self._through([submission.via] if submission.via else [], queue)

        limits = submission.limits
        # From the queue the job ends up in, whose default time limit applies.
        for each in reversed(queues):
            for source_limits in each.limits.values():
                limits = limits.tighten(source_limits)

        hosts = [
            self.hosts[name]
            for name in queues[-1].hosts
            if name in self.hosts and self.hosts[name].usable
        ]
        return limits.tighten(
            Limits(
                memory_bytes=max((host.memory_bytes or 0 for host in hosts), default=0),
                cores=max((host.slots or 0 for host in hosts), default=0),
                gpus=max((host.gpus for host in hosts), default=0),
            )
        )

    def has_gpus(self, queue: Queue) -> bool:
        """
        Whether a host of the queue its jobs end up in has GPUs, usable now or
        not.
        """
        hosts = self._through([], queue)[-1].hosts
        return any(self.hosts[name].has_gpus for name in hosts if name in self.hosts)

    def _through(self, via: list[str], queue: Queue) -> list[Queue]:
        """
        The queues a job goes through from those `via`, by `queue`, to where
        each routing queue sends the user's jobs.
        """
        names = [*via, queue.name]
        while (routed := self.queue(names[-1]).routes_to) and routed not in names:
            names.append(routed)
        return [self.queue(name) for name in names]

    def only_available(self) -> Cluster:
        """The queues that would run the user's jobs, and only their hosts."""
        queues = tuple(queue for queue in self.queues if queue.available)
        used = {name for queue in queues for name in queue.hosts}
        return self.model_copy(
            update={
                "queues": queues,
                "hosts": {
                    name: host for name, host in self.hosts.items() if name in used
                },
            }
        )


def _smallest[T: (int, timedelta)](*values: T | None) -> T | None:
    return min((value for value in values if value is not None), default=None)


@dataclass(frozen=True, slots=True)
class SiteScript:
    """A small script in a site's command directory, and the lines `scan` kept."""

    path: PurePosixPath
    lines: tuple[str, ...]
    # Whether the login shell finds it by its name.
    on_path: bool


# Lines that say which queue a wrapper submits to, how, and which other wrapper a
# dispatcher runs: DTU's LSF wrappers set `QRSH_Q` and `MINLOAD`, its PBS ones
# `QUE` and run `$QSUB -I`, and `linuxsh` execs one of them.
_SCAN_PATTERN = r"^(QRSH_Q|MINLOAD|QUE)=|^[[:space:]]*exec |QSUB"
_PATH_MARKER = "--- PATH"
# Run with the directories as its arguments. Prints each kept line as
# `<path>:<line>`, then for each script the command its name finds on `PATH`.
_SCAN_SCRIPT = f"""
files=$(find -L "$@" -maxdepth 1 -type f -size -64k ! -name '*.old' \\
    -exec grep -I -l -E '{_SCAN_PATTERN}' {{}} + 2>/dev/null)
for file in $files; do
    grep -H -E '{_SCAN_PATTERN}' "$file"
done
echo '{_PATH_MARKER}'
for file in $files; do
    printf '%s\\t%s\\n' "$file" "$(type -P "${{file##*/}}")"
done
exit 0
"""


def resolve_wrappers[T](
    scripts: Sequence[SiteScript], read: Callable[[SiteScript], T | None]
) -> list[tuple[SiteScript, T]]:
    """
    The wrappers among `scripts`, with what `read` makes of each, and the
    dispatchers (such as DTU's `linuxsh`) whose first `exec` runs one of them.
    """
    found = [
        (script, value) for script in scripts if (value := read(script)) is not None
    ]
    by_name: dict[str, T] = {}
    for script, value in found:
        by_name.setdefault(script.path.name, value)
    wrappers = {script.path for script, _ in found}
    for script in scripts:
        targets = [
            words[1]
            for words in map(str.split, script.lines)
            if len(words) > 1 and words[0] == "exec"
        ]
        if script.path in wrappers or not targets:
            continue
        if (value := by_name.get(PurePosixPath(targets[0]).name)) is not None:
            found.append((script, value))
    return found


def scan_command(directories: Sequence[PurePosixPath]) -> list[str]:
    """The command that finds the wrappers in `directories`, for `parse_scan`."""
    return ["bash", "-c", _SCAN_SCRIPT, "scan", *map(str, directories)]


def parse_scan(output: str) -> tuple[SiteScript, ...]:
    kept, _, found = f"\n{output}".partition(f"\n{_PATH_MARKER}\n")
    lines: dict[str, list[str]] = {}
    for line in kept.splitlines():
        path, separator, text = line.partition(":")
        if separator:
            lines.setdefault(path, []).append(text)
    first_found: dict[str, str] = {}
    for line in found.splitlines():
        path, _, command = line.partition("\t")
        first_found[path] = command
    return tuple(
        SiteScript(
            path=PurePosixPath(path),
            lines=tuple(text),
            on_path=first_found.get(path) == path,
        )
        for path, text in lines.items()
    )
