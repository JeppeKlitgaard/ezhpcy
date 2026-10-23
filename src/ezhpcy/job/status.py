"""
What a job, and what is attached to it, is doing, for a display to show while
it runs.

The job (`ezhpcy.job.lifecycle.JobLifecycle`) replaces its `status` with a new
`JobStatus` at each step, from the event loop, and a display reads it from a
thread of its own (see `ezhpcy.console.live_display`). Replaced whole, a status
is never seen half updated, and the event loop never waits for the display,
which waits for the terminal.

Times are `time.monotonic()` readings, so that a display can say how long ago.
"""

import time
from dataclasses import dataclass, field
from enum import Enum, auto

lazy from ezhpcy.app.run import AppStatus
lazy from ezhpcy.forward import ForwardStatus
lazy from ezhpcy.process import ProcessStatus
lazy from ezhpcy.scheduler.base import JobInfo
lazy from ezhpcy.types import Resources


class Phase(Enum):
    """Where the job is, from connecting until it has cleaned up."""

    # The login connection is being made: before the job, for a display that
    # shows it from the start.
    CONNECTING = auto()
    # Provisioning or checking the worker infrastructure, and the control directory.
    PREPARING = auto()
    SUBMITTING = auto()
    # Until the scheduler says the job runs.
    QUEUED = auto()
    # Until the worker's sshd answers.
    STARTING = auto()
    # Entered: things can be attached.
    READY = auto()
    STOPPING = auto()


@dataclass(frozen=True, slots=True)
class PublishedStatus:
    """A Host block: `ssh <alias>` reaches the job's sshd through `port`."""

    alias: str
    port: int
    # Connections open through the port.
    connections: int = 0


type AttachmentStatus = PublishedStatus | AppStatus | ForwardStatus | ProcessStatus


@dataclass(frozen=True, slots=True)
class JobStatus:
    phase: Phase = Phase.PREPARING
    # When the phase began.
    since: float = field(default_factory=time.monotonic)
    job_id: str | None = None
    # The scheduler's latest word on the job.
    job: JobInfo | None = None
    # When the job was first seen running.
    running_since: float | None = None
    # What the job requests, or the interactive submission command that decides
    # instead (see `JobConfig`).
    resources: Resources | None = None
    submission_command: tuple[str, ...] | None = None
    # The worker's sshd, as `host:port`, once it has said which port.
    worker: str | None = None
    last_heartbeat: float | None = None
    # What is attached to the job (its Host blocks), then to its node's host,
    # each oldest first.
    attachments: tuple[AttachmentStatus, ...] = ()
