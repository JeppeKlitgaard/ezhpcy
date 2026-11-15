"""An LSF cluster's queues, from a few commands that each list them all."""

import re
lazy import json
lazy from collections import Counter
lazy from collections.abc import Iterator, Mapping, Sequence
lazy from datetime import datetime, timedelta

from ezhpcy.scheduler.queues import (
    Cluster,
    ExecutionHost,
    GPUMode,
    Limits,
    LimitSource,
    Queue,
    SiteScript,
    Submission,
    Unavailable,
    resolve_wrappers,
)
from ezhpcy.scheduler.types import SchedulerType

_QUEUE_FIELDS = (
    "queue_name description status jl_u runlimit cpulimit memlimit tasklimit"
)
# Host states in which LSF won't start jobs on a host, however free it is.
_UNUSABLE_HOST_STATES = {
    "unavail",
    "unreach",
    "closed_Adm",
    "closed_Lock",
    "closed_LIM",
}
_UNITS = "KMGTPE"
# A limit's label line in `bqueues -l` and `bapp -l` (its value is on the next).
_LIMIT_LABELS = re.compile(
    r"\s*(?:(DEFAULT|MAXIMUM) LIMITS:)?((?:\s*[A-Z_]+LIMIT)+)\s*"
)
_LIMIT_VALUE = re.compile(r"-|[\d.]+(?:\s*min\b(?:\s+of\s+\S+)?|\s*[KMGTPE]B?\b)?")
# The `blimits -c` keys that say whom and what a limit applies to: a limit also
# scoped by anything else (hosts, projects) doesn't bound every job.
_LIMIT_SCOPES = {"NAME", "PER_USER", "USERS", "QUEUES", "PER_QUEUE"}


def commands(user: str) -> dict[str, list[str]]:
    """The commands whose output `parse` reads, by name."""
    return {
        "lsid": ["lsid"],
        "user_groups": ["bugroup", "-w"],
        "host_groups": ["bmgroup", "-w"],
        "hosts": ["bhosts", "-o", "host_name status max", "-json"],
        "host_details": ["lshosts", "-w"],
        "load": ["lsload", "-w", "-I", "mem"],
        "gpus": ["lshosts", "-gpu", "-w"],
        "queues": ["bqueues", "-json", "-o", _QUEUE_FIELDS],
        "queue_details": ["bqueues", "-l"],
        "accessible": ["bqueues", "-u", user, "-json", "-o", "queue_name"],
        "user_limits": ["blimits", "-c"],
        "profiles": ["bapp", "-l"],
        "service_classes": ["bsla"],
        # The unit of memory limits written without one, and the GPU request of a
        # job that makes none.
        "settings": [
            "bash",
            "-c",
            'grep -hE "^(LSF_UNIT_FOR_LIMITS|LSB_GPU_REQ)=" "$LSF_ENVDIR/lsf.conf"; exit 0',
        ],
    }


# Commands that fail where there is nothing to list (e.g. no GPUs, or no queue
# the user may submit to), or that a site may not have.
OPTIONAL = frozenset(commands("").keys() - {"hosts", "queues", "queue_details"})


def parse(
    outputs: Mapping[str, str | None],
    *,
    user: str,
    scripts: tuple[SiteScript, ...],
    generated_at: datetime,
) -> Cluster:
    """The cluster, from the output of `commands`."""
    user_groups = _groups(outputs["user_groups"])
    host_groups = _groups(outputs["host_groups"])
    default_unit = _default_unit(outputs["settings"])
    # `LSB_GPU_REQ`'s, the only GPU request among the settings.
    default_gpu_mode = _gpu_mode(outputs["settings"])
    hosts = _hosts(outputs)
    details = dict(_queue_details(outputs["queue_details"] or ""))
    profiles: dict[str, Limits] = {}
    profile_gpu_modes: dict[str, GPUMode | None] = {}
    for profile, limits, gpu_mode in _profiles(outputs["profiles"] or "", default_unit):
        profiles[profile] = limits
        profile_gpu_modes[profile] = gpu_mode
    service_classes = dict(_service_classes(outputs["service_classes"] or ""))
    accessible = {
        record["QUEUE_NAME"] for record in _records(outputs["accessible"] or "")
    }
    records = _records(outputs["queues"] or "")
    user_limits = _user_limits(
        outputs["user_limits"] or "",
        user,
        user_groups,
        [record["QUEUE_NAME"] for record in records],
        default_unit,
    )

    wrappers: dict[str, list[Submission]] = {}
    for script, (queue, profile, gpu_mode) in resolve_wrappers(scripts, _wrapper):
        wrappers.setdefault(queue, []).append(
            Submission(
                interactive=True,
                command=script.path,
                on_path=script.on_path,
                profile=profile,
                limits=profiles.get(profile, Limits()) if profile else Limits(),
                # The job's own, which neither its profile's nor the queue's
                # overrides.
                gpu_mode=gpu_mode,
            )
        )

    queues = []
    for record in records:
        name = record["QUEUE_NAME"]
        detail = details.get(name, _QueueDetails())
        queue_limits = Limits(
            time_limit=_minutes(record.get("MAX_RUNLIMIT")),
            default_time_limit=detail.default_time_limit,
            cpu_time=_minutes(record.get("MAX_CPULIMIT")),
            memory_bytes=_bytes(record.get("MAX_MEMLIMIT"), default_unit),
            cores=_count(record.get("MAX_TASKLIMIT")),
        )
        own_limits = user_limits.get(name, Limits()).tighten(
            Limits(cores=_count(record.get("JL_U")))
        )
        limits = {
            source: value
            for source, value in (
                (LimitSource.QUEUE, queue_limits),
                (LimitSource.USER, own_limits),
            )
            if value != Limits()
        }

        status = record.get("STATUS", "")
        unavailable = None
        if name not in accessible:
            unavailable = Unavailable.NOT_PERMITTED
        elif status.startswith("Closed"):
            unavailable = Unavailable.CLOSED
        elif status.endswith(":Inact"):
            unavailable = Unavailable.STOPPED
        elif own_limits.jobs == 0 or own_limits.cores == 0:
            unavailable = Unavailable.BLOCKED_BY_LIMIT

        queue_hosts, overflow = _queue_hosts(detail.hosts, host_groups, hosts)
        plain = _plain_submissions(
            name,
            detail.policies,
            queue_limits,
            profiles,
            service_classes,
            user,
            user_groups,
            has_wrappers=name in wrappers,
        )
        has_gpus = any(hosts[host].has_gpus for host in queue_hosts)
        submissions = tuple(
            submission.model_copy(
                update={
                    # As LSF merges GPU requests: the job's, its profile's, the
                    # queue's, the cluster's, then LSF's default.
                    "gpu_mode": (
                        submission.gpu_mode
                        or profile_gpu_modes.get(submission.profile or "")
                        or detail.gpu_mode
                        or default_gpu_mode
                        or "shared"
                    )
                    if has_gpus
                    else None
                }
            )
            for submission in (*plain, *wrappers.get(name, ()))
        )
        queues.append(
            Queue(
                name=name,
                description=record.get("DESCRIPTION") or None,
                unavailable=unavailable,
                hosts=queue_hosts,
                overflow_hosts=overflow,
                exclusive_hosts=detail.exclusive_hosts,
                limits=limits,
                submissions=submissions,
            )
        )

    lsid = outputs["lsid"] or ""
    name = re.search(r"My cluster name is (\S+)", lsid)
    version = re.search(r"LSF\D*?(\d+(?:\.\d+)+)", lsid)
    return Cluster(
        scheduler=SchedulerType.LSF,
        name=name[1] if name else None,
        version=version[1] if version else None,
        user=user,
        generated_at=generated_at,
        default_queue=next(
            (name for name, detail in details.items() if detail.is_default), None
        ),
        hosts=hosts,
        queues=tuple(queues),
    )


def _wrapper(script: SiteScript) -> tuple[str, str | None, GPUMode | None] | None:
    """
    The queue a wrapper submits to, its application profile, and the mode of
    its `-gpu`.
    """
    queues = [
        line.partition("=")[2] for line in script.lines if line.startswith("QRSH_Q=")
    ]
    if not queues:
        return None
    options = [line for line in script.lines if line.startswith("MINLOAD=")]
    profile = re.search(r"-app\s+([^\s'\"]+)", options[-1]) if options else None
    gpus = re.search(r'-gpu\s+"([^"]*)"', options[-1]) if options else None
    return (
        queues[-1].strip("'\""),
        profile[1] if profile else None,
        _gpu_mode(gpus[1]) if gpus else None,
    )


def _plain_submissions(
    name: str,
    policies: str,
    queue_limits: Limits,
    profiles: Mapping[str, Limits],
    service_classes: Mapping[str, list[str] | None],
    user: str,
    user_groups: Mapping[str, list[str]],
    *,
    has_wrappers: bool,
) -> list[Submission]:
    """
    Submissions with `bsub` itself. Where the queue has no time limit, DTU's
    submission filter gives a job a profile by its kind: `serial<queue>` or
    `parallel<queue>`, or their `long` variants for the users of the service
    classes `sc_long<kind><queue>`. Where there is none, it rejects the job, so
    a queue with a wrapper is only for its wrapper. A queue's `<queue>_defapp` is
    its jobs' default profile.
    """
    if f"{name}_defapp" in profiles:
        variants = [f"{name}_defapp"]
    elif queue_limits.time_limit is None:
        variants = [
            profile
            for kind in ("serial", "parallel")
            for profile, users in (
                (f"{kind}{name}", None),
                (f"long{kind}{name}", service_classes.get(f"sc_long{kind}{name}", [])),
            )
            if profile in profiles
            and (users is None or _user_in(users, user, user_groups))
        ]
        if not variants and has_wrappers:
            return []
        variants = variants or [None]
    else:
        variants = [None]

    modes = [
        interactive
        for interactive, allowed in (
            (False, "ONLY_INTERACTIVE" not in policies),
            (True, "NO_INTERACTIVE" not in policies),
        )
        if allowed
    ]
    submissions = []
    for profile in variants:
        limits = profiles[profile] if profile else Limits()
        if profile and profile.startswith(("serial", "longserial")):
            # A serial job has one core.
            limits = limits.tighten(Limits(cores=1))
        submissions.extend(
            Submission(interactive=interactive, profile=profile, limits=limits)
            for interactive in modes
        )
    return submissions


def _hosts(outputs: Mapping[str, str | None]) -> dict[str, ExecutionHost]:
    fields: dict[str, dict] = {}
    for record in _records(outputs["hosts"] or ""):
        fields[record["HOST_NAME"]] = {
            "name": record["HOST_NAME"],
            "usable": record.get("STATUS") not in _UNUSABLE_HOST_STATES,
            "slots": _count(record.get("MAX")),
        }
    for row in _table(outputs["host_details"]):
        if row[0] in fields and len(row) >= 6:
            fields[row[0]].update(
                cores=_count(row[4]), memory_bytes=_bytes(row[5], "M")
            )
    for row in _table(outputs["load"]):
        if row[0] in fields:
            fields[row[0]]["free_memory_bytes"] = _bytes(row[-1], "M")

    models: dict[str, Counter[str]] = {}
    current = None
    for row in _table(outputs["gpus"]):
        # Only a host's first GPU carries the host's name.
        if len(row) == 8:
            current = row[0]
        if current in fields and len(row) >= 7:
            models.setdefault(current, Counter())[row[-6]] += 1
    for host, counted in models.items():
        fields[host].update(
            gpus=counted.total(), gpu_model=counted.most_common(1)[0][0]
        )
    return {name: ExecutionHost(**values) for name, values in fields.items()}


def _queue_hosts(
    tokens: list[str] | None,
    host_groups: Mapping[str, list[str]],
    hosts: Mapping[str, ExecutionHost],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """
    A queue's hosts, from its `HOSTS`, and those of them it only overflows onto:
    those it lists with priority 1, below others.
    """
    if tokens is None or "all" in tokens:
        return tuple(sorted(hosts)), ()
    names = _expand_all(tokens, host_groups)
    own = names
    if any(re.search(r"\+([2-9]|\d\d+)$", token) for token in tokens):
        own = _expand_all(
            [token for token in tokens if not token.endswith("+1")], host_groups
        )
    known = sorted(name for name in names if name in hosts)
    return tuple(known), tuple(name for name in known if name not in own)


class _QueueDetails:
    """What `bqueues -l` says of a queue that `bqueues -o` doesn't."""

    def __init__(self) -> None:
        self.is_default = False
        self.policies = ""
        # `None`: no `HOSTS`, so all of them.
        self.hosts: list[str] | None = None
        self.default_time_limit: timedelta | None = None
        # `EXCLUSIVE` among its policies, without which LSF refuses `bsub -x`.
        self.exclusive_hosts = False
        # Its `GPU_REQ`'s.
        self.gpu_mode: GPUMode | None = None


def _queue_details(output: str) -> Iterator[tuple[str, _QueueDetails]]:
    for block in re.split(r"^QUEUE: ", output, flags=re.MULTILINE)[1:]:
        name, _, body = block.partition("\n")
        details = _QueueDetails()
        lines = body.splitlines()
        details.is_default = bool(lines) and "This is the default queue." in lines[0]
        for line in lines:
            if line.startswith("SCHEDULING POLICIES:"):
                details.policies = line.partition(":")[2]
                details.exclusive_hosts = "EXCLUSIVE" in details.policies.split()
            elif line.startswith("HOSTS:"):
                details.hosts = line.partition(":")[2].split()
            elif line.startswith("GPU_REQ:"):
                details.gpu_mode = _gpu_mode(line)
        details.default_time_limit = _minutes(
            _limits(lines).get(("DEFAULT", "RUNLIMIT"))
        )
        yield name.strip(), details


def _profiles(
    output: str, default_unit: str
) -> Iterator[tuple[str, Limits, GPUMode | None]]:
    """Each application profile, its limits, and the mode of its `GPU_REQ`."""
    for block in re.split(r"^APPLICATION NAME: ", output, flags=re.MULTILINE)[1:]:
        name, _, body = block.partition("\n")
        requests = re.findall(r"^GPU_REQ:(.*)$", body, re.MULTILINE)
        values = {
            label: value
            for (prefix, label), value in _limits(body.splitlines()).items()
            if prefix != "DEFAULT"
        }
        yield (
            name.strip(),
            Limits(
                time_limit=_minutes(values.get("RUNLIMIT")),
                cpu_time=_minutes(values.get("CPULIMIT")),
                memory_bytes=_bytes(values.get("MEMLIMIT"), default_unit),
                cores=_count(values.get("TASKLIMIT")),
            ),
            _gpu_mode(requests[-1]) if requests else None,
        )


def _limits(lines: list[str]) -> dict[tuple[str | None, str], str]:
    """
    The limits of `-l` output, by their `DEFAULT` or `MAXIMUM` prefix (`None` if
    they have none) and label: each is a line of labels, then one of values.
    """
    limits = {}
    for index, line in enumerate(lines):
        match = _LIMIT_LABELS.fullmatch(line)
        if match is None:
            continue
        labels = match[2].split()
        following = next((text for text in lines[index + 1 :] if text.strip()), "")
        values = _LIMIT_VALUE.findall(following)
        if len(labels) == 1:
            values = [following.strip()]
        for label, value in zip(labels, values, strict=False):
            limits[match[1], label] = value
    return limits


def _service_classes(output: str) -> Iterator[tuple[str, list[str] | None]]:
    """Each service class, and its users, `None` for everyone."""
    for block in re.split(r"^SERVICE CLASS NAME:", output, flags=re.MULTILINE)[1:]:
        name, _, body = block.partition("\n")
        users = re.search(r"^ACCESS CONTROL:.*\bUSERS\[([^\]]*)\]", body, re.MULTILINE)
        yield name.strip(), users[1].split() if users else None


def _user_limits(
    output: str,
    user: str,
    user_groups: Mapping[str, list[str]],
    queues: list[str],
    default_unit: str,
) -> dict[str, Limits]:
    """The tightest of the `blimits` limits on the user's jobs, by queue."""
    limits: dict[str, Limits] = {}
    for block in re.findall(
        r"^Begin Limit$(.*?)^End Limit$", output, re.MULTILINE | re.DOTALL
    ):
        values = {}
        for line in block.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip()
        if not values.keys() <= _LIMIT_SCOPES | {
            "SLOTS",
            "MEM",
            "JOBS",
            "ngpus_physical",
        }:
            continue
        users = (values.get("PER_USER") or values.get("USERS") or "all").split()
        if not _user_in(users, user, user_groups):
            continue
        limit = Limits(
            cores=_count(values.get("SLOTS")),
            memory_bytes=_bytes(values.get("MEM"), default_unit),
            jobs=_count(values.get("JOBS")),
            gpus=_count(values.get("ngpus_physical")),
        )
        tokens = (values.get("QUEUES") or values.get("PER_QUEUE") or "all").split()
        for queue in _matching(tokens, queues):
            limits[queue] = limits.get(queue, Limits()).tighten(limit)
    return limits


def _matching(tokens: Sequence[str], names: list[str]) -> list[str]:
    excluded = {token[1:] for token in tokens if token.startswith("~")}
    if "all" in tokens:
        return [name for name in names if name not in excluded]
    return [name for name in names if name in tokens]


def _user_in(
    tokens: Sequence[str], user: str, user_groups: Mapping[str, list[str]]
) -> bool:
    members = _expand_all(tokens, user_groups)
    if "all" in members:
        excluded = [token[1:] for token in tokens if token.startswith("~")]
        return user not in _expand_all(excluded, user_groups)
    return user in members


def _expand_all(
    tokens: Sequence[str],
    groups: Mapping[str, list[str]],
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    """Resolve user or host group tokens (`group/`, `~excluded`, `name+priority`)."""
    included: set[str] = set()
    excluded: set[str] = set()
    for token in tokens:
        names = excluded if token.startswith("~") else included
        name = re.sub(r"\+\d+$", "", token.removeprefix("~"))
        if not name.endswith("/"):
            names.add(name)
        elif (group := name.removesuffix("/")) not in seen:
            names |= _expand_all(groups.get(group, []), groups, seen | {group})
    return included - excluded


def _groups(output: str | None) -> dict[str, list[str]]:
    return {row[0]: row[1:] for row in _table(output)}


def _records(output: str) -> list[dict[str, str]]:
    return json.loads(output).get("RECORDS", []) if output.strip() else []


def _table(output: str | None) -> list[list[str]]:
    """The rows of a table, without its header."""
    lines = (output or "").splitlines()[1:]
    return [line.split() for line in lines if line.strip()]


def _gpu_mode(request: str | None) -> GPUMode | None:
    """The mode of a GPU request, such as `num=1:aff=no:mode=exclusive_process`."""
    match = re.search(r"\bmode=(shared|exclusive_process)\b", request or "")
    if match is None:
        return None
    return "shared" if match[1] == "shared" else "exclusive_process"


def _default_unit(output: str | None) -> str:
    match = re.search(r"LSF_UNIT_FOR_LIMITS\s*=\s*([A-Za-z])", output or "")
    # LSF's own default.
    return match[1].upper() if match else "K"


def _bytes(text: str | None, default_unit: str) -> int | None:
    """A size such as `124.9G`, `32 G` or `16GB`, in `default_unit` without one."""
    match = re.fullmatch(
        r"([\d.]+)\s*([KMGTPE]?)B?", (text or "").strip(), re.IGNORECASE
    )
    if match is None:
        return None
    unit = (match[2] or default_unit).upper()
    return round(float(match[1]) * 1024 ** (_UNITS.index(unit) + 1))


def _minutes(text: str | None) -> timedelta | None:
    """A time limit such as `1440.0 min` or `60.0 min of hostA`."""
    match = re.match(r"\s*([\d.]+)\s*(?:min\b|$)", text or "")
    return timedelta(minutes=float(match[1])) if match else None


def _count(text: str | None) -> int | None:
    """A count, the last of several (as `TASKLIMIT` gives its minimum first)."""
    numbers = re.findall(r"\d+", text or "")
    return int(numbers[-1]) if numbers and "-" not in (text or "") else None
