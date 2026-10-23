"""A job's status panel, as `ezhpcy tunnel` shows it on a terminal."""

import io
from dataclasses import replace

import pytest
from rich.console import Console
from rich.panel import Panel

from ezhpcy.cli._tunnel_status import status_panel
from ezhpcy.job.status import AppStatus, JobStatus, Phase, PublishedStatus
from ezhpcy.scheduler.base import JobInfo, JobState
from ezhpcy.types import Resources

NOW = 10_000.0
TITLE = t"Tunnel {'scratch':name}"
LOGIN = "alice@login.example.com"
STARTING = JobStatus(phase=Phase.CONNECTING, since=NOW)
SERVING = JobStatus(
    phase=Phase.READY,
    since=NOW - 30,
    job_id="42",
    job=JobInfo(state=JobState.RUNNING, raw_state="RUN", host="n-62-11-19"),
    running_since=NOW - 3725,
    resources=Resources.model_validate(
        {
            "queue": "gpua100",
            "cores": 4,
            "gpus": 1,
            "memory": "16GB",
            "time_limit": "4:00",
        }
    ),
    worker="n-62-11-19:40123",
    last_heartbeat=NOW - 3,
    attachments=(PublishedStatus(alias="scratch", port=50123, connections=2),),
)


def panel(status: JobStatus) -> Panel:
    return status_panel(status, title=TITLE, login=LOGIN, now=NOW)


def shown(status: JobStatus, *, width: int = 120) -> list[str]:
    """The panel's lines, without its border, each space between words one."""
    stream = io.StringIO()
    Console(file=stream, width=width, color_system=None).print(panel(status))
    return [
        " ".join(line.strip("│|").split()) for line in stream.getvalue().splitlines()
    ]


def test_while_connecting_it_says_where_to() -> None:
    lines = shown(STARTING)

    assert "Tunnel scratch" in lines[0]
    assert "Connecting to alice@login.example.com (0 s)" in lines
    assert "Press Ctrl+C to stop." in lines


@pytest.mark.parametrize(
    ("pending_reason", "waiting_on"),
    [
        ("Job slot limit reached: 3 hosts", "Job slot limit reached: 3 hosts"),
        # Tersely, if it's a reason we know.
        ("New job is waiting for scheduling", "job scheduling"),
    ],
)
def test_while_queued_it_says_what_the_job_waits_on(
    pending_reason: str, waiting_on: str
) -> None:
    status = JobStatus(
        phase=Phase.QUEUED,
        since=NOW - 65,
        job_id="42",
        job=JobInfo(
            state=JobState.PENDING, raw_state="PEND", pending_reason=pending_reason
        ),
    )

    lines = shown(status)

    assert "Waiting for the worker job to start (1 min 5 s)" in lines
    assert "Job 42 (PEND)" in lines
    assert f"Waiting on {waiting_on}" in lines


def test_once_serving_it_says_how_to_connect_and_how_the_worker_does() -> None:
    lines = shown(SERVING)

    assert "Connect with ssh scratch" in lines
    # The job and the worker, beside what the job requests.
    first = lines.index("Job 42 (RUN) Queue gpua100")
    assert lines[first : first + 5] == [
        "Job 42 (RUN) Queue gpua100",
        "Worker n-62-11-19:40123 Cores 4",
        "Running for 1 h 2 min of 4 h 0 min GPUs 1",
        "Heartbeat 3 s ago Memory 16.0 GB",
        "Connections 2 Time limit 4 h 0 min",
    ]
    assert "Press Ctrl+C to stop." in lines


def test_in_a_narrow_terminal_the_resources_go_below() -> None:
    lines = shown(SERVING, width=60)

    first = lines.index("Job 42 (RUN)")
    assert lines[first : first + 11] == [
        "Job 42 (RUN)",
        "Worker n-62-11-19:40123",
        "Running for 1 h 2 min of 4 h 0 min",
        "Heartbeat 3 s ago",
        "Connections 2",
        "",
        "Queue gpua100",
        "Cores 4",
        "GPUs 1",
        "Memory 16.0 GB",
        "Time limit 4 h 0 min",
    ]


def test_an_interactive_submission_command_is_shown_instead_of_the_resources() -> None:
    """The command decides the resources, so those configured mean nothing."""
    lines = shown(replace(SERVING, submission_command=("a100sh",)))

    assert "Job 42 (RUN) Command a100sh" in lines
    assert "Running for 1 h 2 min" in lines
    assert not any("Cores" in line for line in lines)


def test_while_stopping_it_says_how_to_force_quit_in_red() -> None:
    status = JobStatus(phase=Phase.STOPPING, since=NOW - 2)

    lines = shown(status)

    assert "Stopping (2 s)" in lines
    assert "(press Ctrl+C again to force quit)" in lines
    assert "Press Ctrl+C to stop." not in lines
    assert panel(status).border_style == "red"


def test_the_first_attachment_says_how_to_reach_it_and_all_count() -> None:
    status = replace(
        SERVING,
        attachments=(
            AppStatus(name="lab", url="http://127.0.0.1:8888/lab", connections=1),
            PublishedStatus(alias="scratch", port=50123, connections=2),
        ),
    )

    lines = shown(status)

    assert "Open http://127.0.0.1:8888/lab" in lines
    assert any(line.startswith("Connections 3") for line in lines)


def test_ready_with_nothing_attached_is_not_yet_serving() -> None:
    status = replace(SERVING, attachments=())

    lines = shown(status)

    assert "Ready (30 s)" in lines
    assert not any(line.startswith("Connections") for line in lines)
    assert panel(status).border_style == "yellow"
