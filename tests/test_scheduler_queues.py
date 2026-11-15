"""Tests for describing a cluster's queues (`Scheduler.describe_queues`)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import pytest

from ezhpcy.scheduler.base import SchedulerError
from ezhpcy.scheduler.lsf import LSFScheduler
from ezhpcy.scheduler.pbs import PBSScheduler
from ezhpcy.scheduler.queues import (
    Cluster,
    ExecutionHost,
    Limits,
    LimitSource,
    Queue,
    Submission,
    Unavailable,
    parse_scan,
)
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.ssh import CommandFailed
from ezhpcy.types import LSFOptions, SiteConfig
from tests.support.scheduler import BatchShell

_GIB = 1024**3
_SITE = PurePosixPath("/site/bin")


_SCAN = f"""\
{_SITE}/qrsh:QRSH_Q=interactive
{_SITE}/qrsh:MINLOAD='-app qrsh'
{_SITE}/gpush:QRSH_Q=gpu
{_SITE}/gpush:MINLOAD='-app gpush -gpu "num=2:mode=exclusive_process"'
{_SITE}/gpuappsh:QRSH_Q=gpu
{_SITE}/gpuappsh:MINLOAD='-app gpush'
{_SITE}/linuxsh:    exec pbslinuxsh $@
{_SITE}/linuxsh:    exec qrsh $@
{_SITE}/pbslinuxsh:QSUB=qsub
{_SITE}/pbslinuxsh:QUE=workq
{_SITE}/pbslinuxsh:$QSUB -I -q $QUE -N $NAME $@
{_SITE}/notawrapper:exec true
--- PATH
{_SITE}/qrsh\t{_SITE}/qrsh
{_SITE}/gpush\t{_SITE}/gpush
{_SITE}/gpuappsh\t{_SITE}/gpuappsh
{_SITE}/linuxsh\t/other/bin/linuxsh
{_SITE}/pbslinuxsh\t{_SITE}/pbslinuxsh
{_SITE}/notawrapper\t
"""


def _records(*records: dict[str, str]) -> str:
    return json.dumps({"COMMAND": "test", "RECORDS": list(records)})


def _queue_record(name: str, **fields: str) -> dict[str, str]:
    record = {
        "QUEUE_NAME": name,
        "DESCRIPTION": f"the {name} queue",
        "STATUS": "Open:Active",
        "JL_U": "",
        "MAX_RUNLIMIT": "",
        "MAX_CPULIMIT": "",
        "MAX_MEMLIMIT": "",
        "MAX_TASKLIMIT": "",
    }
    return record | fields


_LSF = {
    r"^lsid": (
        "IBM Spectrum LSF Standard 10.1.0.16, Jul 27 2026\n\n"
        "My cluster name is testcluster\nMy master name is master\n"
    ),
    r"^bugroup": "GROUP_NAME    USERS\nstaff    alice bob\nlongusers    carol\n",
    r"^bmgroup": "GROUP_NAME    HOSTS\ncpus   node1 node2\nspare   node3\n",
    r"^bhosts": _records(
        {"HOST_NAME": "node1", "STATUS": "ok", "MAX": "24"},
        {"HOST_NAME": "node2", "STATUS": "closed_Full", "MAX": "24"},
        {"HOST_NAME": "node3", "STATUS": "unavail", "MAX": "24"},
        {"HOST_NAME": "gpu1", "STATUS": "ok", "MAX": "32"},
    ),
    r"^lshosts -w": (
        "HOST_NAME  type  model  cpuf ncpus maxmem maxswp server RESOURCES\n"
        "node1  X86_64 Gold 1.0 24 251.5G 4G Yes (avx)\n"
        "node2  X86_64 Gold 1.0 24 251.5G 4G Yes (avx)\n"
        "node3  X86_64 Gold 1.0 24 251.5G 4G Yes (avx)\n"
        "gpu1   X86_64 Gold 1.0 16 377G 4G Yes (avx)\n"
        "login  X86_64 Gold 1.0 - - - No ()\n"
    ),
    r"^lsload": "HOST_NAME status mem\nnode1 ok 200.0G\ngpu1 ok 300G\n",
    r"^lshosts -gpu": (
        "HOST_NAME gpu_id gpu_model gpu_driver gpu_factor numa_id vendor mig\n"
        "gpu1 0 TeslaV100 580.1 7.0 0 Nvidia N\n"
        "     1 TeslaV100 580.1 7.0 1 Nvidia N\n"
    ),
    r"^bqueues -json": _records(
        _queue_record("hpc", MAX_TASKLIMIT="16"),
        _queue_record("gpu", MAX_RUNLIMIT="1440.0 min", JL_U="12"),
        _queue_record("interactive"),
        _queue_record("closed", MAX_RUNLIMIT="60.0 min", STATUS="Closed:Adm"),
        _queue_record("private", MAX_RUNLIMIT="60.0 min"),
        _queue_record("limited", MAX_RUNLIMIT="60.0 min"),
    ),
    r"^bqueues -u alice": _records(
        *(
            {"QUEUE_NAME": name}
            for name in ("hpc", "gpu", "interactive", "closed", "limited")
        )
    ),
    r"^bqueues -l": """
QUEUE: hpc
  -- the hpc queue  This is the default queue.

SCHEDULING POLICIES:  FAIRSHARE  EXCLUSIVE  NO_INTERACTIVE
USERS: all
HOSTS:  cpus/+100 spare/+1

-------------------------------------------------------------------------------

QUEUE: gpu
  -- the gpu queue

DEFAULT LIMITS: RUNLIMIT
 15.0 min

MAXIMUM LIMITS: RUNLIMIT
 1440.0 min

HOSTS:  gpu1
GPU_REQ:  "num=1:aff=no:mode=exclusive_process"

QUEUE: interactive
  -- the interactive queue
HOSTS:  node1

QUEUE: closed
  -- the closed queue
HOSTS:  all

QUEUE: private
  -- the private queue
HOSTS:  node2

QUEUE: limited
  -- the limited queue
""",
    r"^blimits": """
Begin Limit
  NAME 		= staff_gpus
  PER_USER 	= staff/
  QUEUES 	= gpu
  SLOTS    	= 8
  MEM      	= 65536
  ngpus_physical 	= 2
End Limit

Begin Limit
  NAME 		= nobody
  PER_USER 	= all ~bob
  QUEUES 	= limited
  JOBS     	= 0
End Limit

Begin Limit
  NAME 		= per_host
  PER_HOST 	= node1
  QUEUES 	= all
  SLOTS    	= 1
End Limit
""",
    r"^bapp": """
APPLICATION NAME: serialhpc
 -- serial jobs

STATISTICS:
   NJOBS     PEND      RUN    SSUSP    USUSP      RSV
       0        0        0        0        0        0

PARAMETERS:
 RUNLIMIT
 7200.0 min

-------------------------------------------------------------------------------
APPLICATION NAME: parallelhpc
 -- parallel jobs

PARAMETERS:
 RUNLIMIT
 4320.0 min
 TASKLIMIT
 1 8

-------------------------------------------------------------------------------
APPLICATION NAME: longserialhpc
 -- long serial jobs

PARAMETERS:
 RUNLIMIT
 43200.0 min

-------------------------------------------------------------------------------
APPLICATION NAME: qrsh
 -- interactive shells

PARAMETERS:
 CPULIMIT
 1440.0 min
 RUNLIMIT
 2880.0 min

 MEMLIMIT
    32 G

-------------------------------------------------------------------------------
APPLICATION NAME: gpush
 -- interactive GPU shells

GPU_REQ: num=1:aff=no:mode=shared
""",
    r"^bsla": """
SERVICE CLASS NAME:  sc_longserialhpc
 -- long serial jobs for the hpc queue
ACCESS CONTROL:      QUEUES[hpc] USERS[longusers/]
AUTO ATTACH:         N
""",
    r"LSF_UNIT_FOR_LIMITS": "LSF_UNIT_FOR_LIMITS=MB\n",
    r"find -L": _SCAN,
}


_PBS = {
    r"qstat -Bf": json.dumps(
        {
            "pbs_version": "20.0.0",
            "pbs_server": "server1",
            "Server": {
                "server1": {
                    "default_queue": "workq",
                    "resources_max": {"mem": "256gb"},
                    "resources_default": {"walltime": "01:00:00"},
                }
            },
        }
    ),
    r"qstat -Qf": json.dumps(
        {
            "Queue": {
                "workq": {
                    "queue_type": "Route",
                    "route_destinations": "staffq,openq",
                    "enabled": "True",
                    "started": "True",
                },
                "staffq": {
                    "queue_type": "Execution",
                    "acl_group_enable": "True",
                    "acl_groups": "+,-guests",
                    "resources_max": {"cput": "24:00:00"},
                    "enabled": "True",
                    "started": "True",
                },
                "openq": {
                    "queue_type": "Execution",
                    "acl_group_enable": "True",
                    "acl_groups": "users",
                    "resources_max": {"mem": "64gb"},
                    "resources_default": {"walltime": "72:00:00"},
                    "enabled": "True",
                    "started": "True",
                },
                "stoppedq": {
                    "queue_type": "Execution",
                    "enabled": "True",
                    "started": "False",
                },
                "brokenq": {"total_jobs": 0},
            }
        }
    ),
    r"pbsnodes -a": json.dumps(
        {
            "nodes": {
                "n1": {
                    "state": "free",
                    "pcpus": 24,
                    "queue": "openq",
                    "resources_available": {"ncpus": 48, "mem": "384259mb"},
                    "resources_assigned": {"mem": "134217728kb"},
                },
                "n2": {
                    "state": "state-unknown,down,offline",
                    "pcpus": 80,
                    "queue": "openq",
                    "resources_available": {"ncpus": 80, "mem": "500gb"},
                },
                "n3": {
                    "state": "free",
                    "pcpus": 8,
                    "resources_available": {"ncpus": 8, "ngpus": 1, "mem": "64gb"},
                },
                "n4": {
                    "state": "free",
                    "queue": "staffq",
                    "sharing": "ignore_excl",
                    "resources_available": {"ncpus": 4, "mem": "8gb"},
                },
            }
        }
    ),
    r"^id -Gn": "users guests\n",
    r"find -L": _SCAN,
}


def _lsf(shell: Any, site: SiteConfig) -> LSFScheduler:
    return LSFScheduler(shell, LSFOptions(), site)


def _pbs(shell: Any, site: SiteConfig) -> PBSScheduler:
    return PBSScheduler(shell, site)


async def _describe_lsf(**options: Any) -> Cluster:
    scheduler = _lsf(BatchShell(_LSF), SiteConfig(wrapper_directories=(_SITE,)))
    return await scheduler.describe_queues(**options)


async def _describe_pbs(**options: Any) -> Cluster:
    site = SiteConfig(
        wrapper_directories=(_SITE,),
        pbs_command_directory=PurePosixPath("/opt/pbs/bin"),
    )
    return await _pbs(BatchShell(_PBS), site).describe_queues(**options)


# The batch of commands


async def test_commands_run_in_one_login_shell():
    shell = BatchShell({"^first": "one\ntwo\n", "^second": (3, "", "it broke\n")})
    scheduler = _lsf(shell, SiteConfig())

    outputs = await scheduler._run_all(
        {"a": ["first", "x y"], "b": ["second"], "c": ["third"]},
        optional={"b", "c"},
    )

    assert outputs == {"a": "one\ntwo\n", "b": None, "c": None}
    assert shell.commands == [["first", "x y"], ["second"], ["third"]]


async def test_a_required_command_that_fails_says_why():
    scheduler = _lsf(BatchShell({}), SiteConfig())

    with pytest.raises(SchedulerError, match="missing: command not found") as raised:
        await scheduler._run_all({"a": ["missing", "--flag"]})

    assert isinstance(raised.value.__cause__, CommandFailed)
    assert raised.value.__cause__.exit_status == 127


def test_the_scan_keeps_each_scripts_lines_and_whether_path_finds_it():
    scripts = {script.path.name: script for script in parse_scan(_SCAN)}

    assert scripts["qrsh"].lines == ("QRSH_Q=interactive", "MINLOAD='-app qrsh'")
    assert scripts["qrsh"].on_path
    assert not scripts["linuxsh"].on_path
    assert not scripts["notawrapper"].on_path
    assert parse_scan("--- PATH\n") == ()


# LSF


async def test_lsf_cluster_and_hosts():
    cluster = await _describe_lsf()

    assert (cluster.scheduler, cluster.name, cluster.version) == (
        SchedulerType.LSF,
        "testcluster",
        "10.1.0.16",
    )
    assert cluster.user == "alice"
    assert cluster.default_queue == "hpc"
    assert cluster.hosts["node1"] == ExecutionHost(
        name="node1",
        usable=True,
        slots=24,
        cores=24,
        memory_bytes=round(251.5 * _GIB),
        free_memory_bytes=200 * _GIB,
    )
    assert cluster.hosts["node2"].usable  # full, but not closed
    assert not cluster.hosts["node3"].usable
    assert (cluster.hosts["gpu1"].gpus, cluster.hosts["gpu1"].gpu_model) == (
        2,
        "TeslaV100",
    )


async def test_lsf_queue_availability():
    cluster = await _describe_lsf()

    assert {queue.name: queue.unavailable for queue in cluster.queues} == {
        "hpc": None,
        "gpu": None,
        "interactive": None,
        "closed": Unavailable.CLOSED,
        "private": Unavailable.NOT_PERMITTED,
        # A limit of no jobs for everyone but bob.
        "limited": Unavailable.BLOCKED_BY_LIMIT,
    }


async def test_lsf_queue_limits_and_hosts():
    cluster = await _describe_lsf()
    hpc, gpu = cluster.queue("hpc"), cluster.queue("gpu")

    assert hpc.hosts == ("node1", "node2", "node3")
    assert hpc.overflow_hosts == ("node3",)
    assert hpc.limits == {LimitSource.QUEUE: Limits(cores=16)}
    # The per-host limit doesn't bound every job, so it isn't among them.
    assert gpu.limits == {
        LimitSource.QUEUE: Limits(
            time_limit=timedelta(days=1), default_time_limit=timedelta(minutes=15)
        ),
        LimitSource.USER: Limits(memory_bytes=64 * _GIB, cores=8, gpus=2),
    }
    assert cluster.queue("limited").hosts == tuple(sorted(cluster.hosts))


async def test_lsf_submissions():
    cluster = await _describe_lsf()

    # No time limit: a profile for each kind of job, but not the long one,
    # whose service class alice isn't in. No interactive jobs.
    assert cluster.queue("hpc").submissions == (
        Submission(
            interactive=False,
            profile="serialhpc",
            limits=Limits(time_limit=timedelta(days=5), cores=1),
        ),
        Submission(
            interactive=False,
            profile="parallelhpc",
            limits=Limits(time_limit=timedelta(days=3), cores=8),
        ),
    )
    # GPU modes as LSF merges GPU requests: the wrapper's `-gpu` (`gpush`), then
    # its profile's (`gpuappsh`), then the queue's.
    assert cluster.queue("gpu").submissions == (
        Submission(interactive=False, gpu_mode="exclusive_process"),
        Submission(interactive=True, gpu_mode="exclusive_process"),
        Submission(
            interactive=True,
            command=_SITE / "gpush",
            profile="gpush",
            gpu_mode="exclusive_process",
        ),
        Submission(
            interactive=True,
            command=_SITE / "gpuappsh",
            profile="gpush",
            gpu_mode="shared",
        ),
    )
    # No time limit and no profile, but a wrapper: only the wrapper. `linuxsh`
    # runs PBS's wrapper first, so it isn't one of LSF's.
    assert cluster.queue("interactive").submissions == (
        Submission(
            interactive=True,
            command=_SITE / "qrsh",
            profile="qrsh",
            limits=Limits(
                time_limit=timedelta(days=2),
                cpu_time=timedelta(days=1),
                memory_bytes=32 * _GIB,
            ),
        ),
    )


async def test_lsf_exclusive_hosts_and_default_gpu_modes():
    cluster = await _describe_lsf()

    assert [queue.name for queue in cluster.queues if queue.exclusive_hosts] == ["hpc"]
    # Its hosts have no GPUs.
    assert {s.gpu_mode for s in cluster.queue("hpc").submissions} == {None}
    # All hosts, `gpu1` among them, and no GPU request: LSF's default.
    assert {s.gpu_mode for s in cluster.queue("limited").submissions} == {"shared"}


async def test_lsf_cluster_gpu_request_gives_the_default_gpu_mode():
    settings = 'LSF_UNIT_FOR_LIMITS=MB\nLSB_GPU_REQ="num=1:mode=exclusive_process"\n'
    shell = BatchShell(_LSF | {r"LSF_UNIT_FOR_LIMITS": settings})

    cluster = await _lsf(shell, SiteConfig()).describe_queues()

    assert {s.gpu_mode for s in cluster.queue("limited").submissions} == {
        "exclusive_process"
    }


async def test_lsf_request_limits():
    cluster = await _describe_lsf()
    gpu = cluster.queue("gpu")

    assert cluster.request_limits(gpu, gpu.submissions[0]) == Limits(
        time_limit=timedelta(days=1),
        default_time_limit=timedelta(minutes=15),
        memory_bytes=64 * _GIB,
        cores=8,
        gpus=2,
    )


async def test_lsf_only_available_to_me():
    cluster = await _describe_lsf(only_available_to_me=True)

    assert [queue.name for queue in cluster.queues] == ["hpc", "gpu", "interactive"]
    assert set(cluster.hosts) == {"node1", "node2", "node3", "gpu1"}


async def test_lsf_without_optional_listings():
    responses = {
        pattern: response
        for pattern, response in _LSF.items()
        if pattern in {r"^bhosts", r"^bqueues -json", r"^bqueues -l"}
    }
    scheduler = _lsf(BatchShell(responses), SiteConfig())

    cluster = await scheduler.describe_queues()

    # Nothing says alice may use any queue.
    assert all(
        queue.unavailable is Unavailable.NOT_PERMITTED for queue in cluster.queues
    )
    assert cluster.hosts["gpu1"].gpus == 0


# PBS


async def test_pbs_cluster_and_hosts():
    cluster = await _describe_pbs()

    assert (cluster.scheduler, cluster.name, cluster.version) == (
        SchedulerType.PBS,
        "server1",
        "20.0.0",
    )
    assert cluster.default_queue == "workq"
    assert cluster.hosts["n1"] == ExecutionHost(
        name="n1",
        usable=True,
        slots=48,
        cores=24,
        memory_bytes=384259 * 1024**2,
        free_memory_bytes=384259 * 1024**2 - 128 * _GIB,
    )
    assert not cluster.hosts["n2"].usable
    assert cluster.hosts["n3"].gpus == 1


async def test_pbs_queue_availability_and_routing():
    cluster = await _describe_pbs()

    assert {
        queue.name: (queue.unavailable, queue.routes_to) for queue in cluster.queues
    } == {
        # alice is in `guests`, which `staffq` excludes, so it goes to `openq`.
        "workq": (None, "openq"),
        "staffq": (Unavailable.NOT_PERMITTED, None),
        "openq": (None, None),
        "stoppedq": (Unavailable.STOPPED, None),
        "brokenq": (Unavailable.CLOSED, None),
    }
    # No node is assigned to it, so it runs jobs on the unassigned ones.
    assert cluster.queue("stoppedq").hosts == ("n3",)
    assert cluster.queue("openq").hosts == ("n1", "n2")
    assert cluster.queue("workq").hosts == ()


async def test_pbs_submissions():
    cluster = await _describe_pbs()
    plain = (Submission(interactive=False), Submission(interactive=True))
    wrappers = (
        Submission(interactive=True, command=_SITE / "pbslinuxsh"),
        Submission(interactive=True, command=_SITE / "linuxsh", on_path=False),
    )

    assert cluster.queue("workq").submissions == (*wrappers, *plain)
    assert cluster.queue("openq").submissions == (
        *plain,
        *(
            submission.model_copy(update={"via": "workq"})
            for submission in (*wrappers, *plain)
        ),
    )


async def test_pbs_exclusive_hosts():
    cluster = await _describe_pbs()

    # `staffq`'s node ignores `place=excl`; `workq` sends alice's jobs to `openq`.
    assert {queue.name: queue.exclusive_hosts for queue in cluster.queues} == {
        "workq": True,
        "staffq": False,
        "openq": True,
        "stoppedq": True,
        "brokenq": True,
    }
    assert all(
        submission.gpu_mode is None
        for queue in cluster.queues
        for submission in queue.submissions
    )


async def test_pbs_request_limits_follow_the_route():
    cluster = await _describe_pbs()
    workq = cluster.queue("workq")

    assert cluster.queue("openq").limits == {
        LimitSource.QUEUE: Limits(
            memory_bytes=64 * _GIB, default_time_limit=timedelta(hours=72)
        ),
        LimitSource.SERVER: Limits(
            memory_bytes=256 * _GIB, default_time_limit=timedelta(hours=1)
        ),
    }
    # The offline node doesn't count.
    assert cluster.request_limits(workq, workq.submissions[0]) == Limits(
        default_time_limit=timedelta(hours=72),
        memory_bytes=64 * _GIB,
        cores=48,
        gpus=0,
    )


# The model


def test_a_queue_has_gpus_where_its_jobs_end_up():
    cluster = Cluster(
        scheduler=SchedulerType.PBS,
        user="alice",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        hosts={
            "cpu": ExecutionHost(name="cpu", usable=True),
            "gpu": ExecutionHost(name="gpu", usable=False, gpus=4),
        },
        queues=(
            Queue(name="route", routes_to="gpuq"),
            Queue(name="gpuq", hosts=("gpu",)),
            Queue(name="cpuq", hosts=("cpu", "gone")),
        ),
    )

    assert cluster.hosts["gpu"].has_gpus
    assert not cluster.hosts["cpu"].has_gpus
    # Usable or not.
    assert cluster.has_gpus(cluster.queue("route"))
    assert not cluster.has_gpus(cluster.queue("cpuq"))


def test_a_queue_without_usable_hosts_allows_nothing():
    cluster = Cluster(
        scheduler=SchedulerType.PBS,
        user="alice",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        hosts={
            "down": ExecutionHost(name="down", usable=False, slots=8, memory_bytes=_GIB)
        },
        queues=(Queue(name="q", hosts=("down",)),),
    )
    queue = cluster.queue("q")

    assert cluster.request_limits(queue, Submission(interactive=False)) == Limits(
        memory_bytes=0, cores=0, gpus=0
    )
    with pytest.raises(KeyError):
        cluster.queue("missing")
