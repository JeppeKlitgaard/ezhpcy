"""A PBS Pro cluster's queues, from its JSON listings of them and its nodes."""

import re
lazy import json
lazy from collections.abc import Mapping
lazy from datetime import datetime, timedelta

from ezhpcy.scheduler.queues import (
    Cluster,
    ExecutionHost,
    Limits,
    LimitSource,
    Queue,
    SiteScript,
    Submission,
    Unavailable,
    resolve_wrappers,
)
from ezhpcy.scheduler.types import SchedulerType

# Node states in which PBS won't start jobs on a node, however free it is.
_UNUSABLE_NODE_STATES = {
    "down",
    "offline",
    "state-unknown",
    "stale",
    "unresolvable",
    "maintenance",
}
_UNITS = "kmgtp"


def commands(executable: Mapping[str, str]) -> dict[str, list[str]]:
    """
    The commands whose output `parse` reads, by name, `executable` giving each
    PBS command's path.
    """
    return {
        "server": [executable["qstat"], "-Bf", "-F", "json"],
        "queues": [executable["qstat"], "-Qf", "-F", "json"],
        "nodes": [executable["pbsnodes"], "-a", "-F", "json"],
        "groups": ["id", "-Gn"],
    }


def parse(
    outputs: Mapping[str, str | None],
    *,
    user: str,
    scripts: tuple[SiteScript, ...],
    generated_at: datetime,
) -> Cluster:
    """The cluster, from the output of `commands`."""
    groups = set((outputs["groups"] or "").split())
    status = json.loads(outputs["server"] or "{}")
    server_name, server = next(iter(status.get("Server", {}).items()), (None, {}))
    records: dict[str, dict] = json.loads(outputs["queues"] or "{}").get("Queue", {})
    nodes: dict[str, dict] = json.loads(outputs["nodes"] or "{}").get("nodes", {})

    destinations = {
        name: _destination(record, records, user, groups)
        for name, record in records.items()
        if record.get("queue_type") == "Route"
    }
    submissions: dict[str, list[Submission]] = {}
    for script, queue in resolve_wrappers(scripts, _wrapper):
        submissions.setdefault(queue, []).append(
            Submission(interactive=True, command=script.path, on_path=script.on_path)
        )
    for name in records:
        submissions.setdefault(name, []).extend(
            Submission(interactive=interactive) for interactive in (False, True)
        )
    # A routing queue's ways in also lead to where it routes the user's jobs.
    for route, destination in destinations.items():
        if destination is not None:
            submissions[destination].extend(
                submission.model_copy(update={"via": route})
                for submission in submissions[route]
            )

    queue_hosts: dict[str, list[str]] = {}
    for name, record in records.items():
        if record.get("queue_type") == "Route":
            queue_hosts[name] = []
            continue
        # A queue no node is assigned to runs jobs on the unassigned ones.
        hosts = [node for node, fields in nodes.items() if fields.get("queue") == name]
        queue_hosts[name] = hosts or [
            node for node, fields in nodes.items() if "queue" not in fields
        ]

    server_limits = _limits(server)
    queues = []
    for name, record in records.items():
        is_route = record.get("queue_type") == "Route"
        unavailable = _rejection(record, user, groups)
        if unavailable is None and is_route and destinations[name] is None:
            unavailable = Unavailable.NO_ROUTE
        destination = destinations.get(name)
        target = records[destination] if destination else record
        if unavailable is None and target.get("started") != "True":
            unavailable = Unavailable.STOPPED

        hosts = queue_hosts[name]
        # Where its jobs run: a routing queue's are its destination's.
        running = queue_hosts[destination] if destination else hosts
        limits = {
            source: value
            for source, value in (
                (LimitSource.QUEUE, _limits(record)),
                (LimitSource.SERVER, server_limits),
            )
            if value != Limits()
        }
        queues.append(
            Queue(
                name=name,
                unavailable=unavailable,
                routes_to=destination,
                hosts=tuple(sorted(hosts)),
                # A node's `sharing` that ignores `place=excl` shares it anyway.
                exclusive_hosts=bool(running)
                and all(
                    nodes[node].get("sharing", "default_shared") != "ignore_excl"
                    for node in running
                ),
                limits=limits,
                submissions=tuple(submissions[name]),
            )
        )

    return Cluster(
        scheduler=SchedulerType.PBS,
        name=server_name,
        version=status.get("pbs_version"),
        user=user,
        generated_at=generated_at,
        default_queue=server.get("default_queue"),
        hosts={name: _host(name, fields) for name, fields in nodes.items()},
        queues=tuple(queues),
    )


def _wrapper(script: SiteScript) -> str | None:
    """The queue a wrapper submits an interactive job to (`$QSUB -I -q $QUE`)."""
    queues = [
        line.partition("=")[2] for line in script.lines if line.startswith("QUE=")
    ]
    interactive = any(re.search(r"QSUB\b.*\s-I\b", line) for line in script.lines)
    return queues[-1].strip("'\"") if queues and interactive else None


def _host(name: str, fields: dict) -> ExecutionHost:
    available = fields.get("resources_available", {})
    assigned = fields.get("resources_assigned", {})
    memory = _bytes(available.get("mem"))
    return ExecutionHost(
        name=name,
        usable=not _UNUSABLE_NODE_STATES & set(str(fields.get("state", "")).split(",")),
        slots=_count(available.get("ncpus")),
        cores=_count(fields.get("pcpus")),
        memory_bytes=memory,
        free_memory_bytes=(
            None if memory is None else memory - (_bytes(assigned.get("mem")) or 0)
        ),
        gpus=_count(available.get("ngpus")) or 0,
    )


def _limits(record: Mapping) -> Limits:
    maximum = record.get("resources_max", {})
    default = record.get("resources_default", {})
    return Limits(
        time_limit=_duration(maximum.get("walltime")),
        default_time_limit=_duration(default.get("walltime")),
        cpu_time=_duration(maximum.get("cput")),
        memory_bytes=_bytes(maximum.get("mem")),
        cores=_count(maximum.get("ncpus")),
        gpus=_count(maximum.get("ngpus")),
    )


def _destination(
    record: Mapping, records: Mapping[str, Mapping], user: str, groups: set[str]
) -> str | None:
    """The first destination that takes the user's jobs, as PBS routes them."""
    if _rejection(record, user, groups) is not None:
        return None
    for destination in str(record.get("route_destinations", "")).split(","):
        if (
            destination in records
            and _rejection(records[destination], user, groups) is None
        ):
            return destination
    return None


def _rejection(record: Mapping, user: str, groups: set[str]) -> Unavailable | None:
    """Why the queue refuses the user's jobs, routed ones too."""
    if record.get("enabled") != "True":
        return Unavailable.CLOSED
    for kind, names in (("user", {user}), ("group", groups)):
        if record.get(f"acl_{kind}_enable") == "True" and not _acl_allows(
            str(record.get(f"acl_{kind}s", "")), names
        ):
            return Unavailable.NOT_PERMITTED
    return None


def _acl_allows(entries: str, names: set[str]) -> bool:
    """
    Apply a PBS access list (`+,-group`: everyone but `group`): an entry naming
    one of `names` beats a bare `+` or `-`, and of those, a deny beats an allow.
    """
    matched: set[str] = set()
    everyone = False
    for entry in filter(None, (entry.strip() for entry in entries.split(","))):
        sign, name = (entry[0], entry[1:]) if entry[0] in "+-" else ("+", entry)
        if not name:
            everyone = sign == "+"
        elif name.partition("@")[0] in names:
            matched.add(sign)
    if "-" in matched:
        return False
    return "+" in matched or everyone


def _bytes(text: object) -> int | None:
    """A PBS size, such as `128308mb` or `16gb`, whose units are binary."""
    match = re.fullmatch(r"(\d+)([kmgtp]?)([bw]?)", str(text or ""), re.IGNORECASE)
    if match is None:
        return None
    prefix = match[2].lower()
    exponent = _UNITS.index(prefix) + 1 if prefix else 0
    word = 8 if match[3].lower() == "w" else 1
    return int(match[1]) * 1024**exponent * word


def _duration(text: object) -> timedelta | None:
    """A PBS duration, `[[hours:]minutes:]seconds`."""
    text = str(text or "")
    if not re.fullmatch(r"\d+(:\d+){0,2}(\.\d+)?", text):
        return None
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return timedelta(seconds=seconds)


def _count(value: object) -> int | None:
    text = "" if value is None else str(value)
    return int(text) if text.isdigit() else None
