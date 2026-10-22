"""
A worker job: a node running the worker's sshd, reached through the login
connection, with things attached to it while it runs.

- `lifecycle` runs one job, from submitting it until everything is cleaned up,
  as an async context manager: entering returns once the job is ready, leaving
  cleans up.
- `attachments` registers what is attached to a running job, so that it is
  closed once, early or when the job is left.

How a job's end shows to the code that uses it (see `JobLifecycle`): when the
job ends or fails while that code runs, the code is cancelled, and leaving the
job raises `JobEnded` or `JobFailed`; `JobLifecycle.wait` returns a `JobEnd`
instead, or raises `JobFailed`.
"""

from dataclasses import dataclass
lazy from string.templatelib import Template

from ezhpcy.messages import MessageError


@dataclass(frozen=True, slots=True)
class JobEnd:
    """How a job ended without failing."""

    # Why it ended, as its log says.
    reason: str
    # Whether the scheduler ended it (it exited, or hit its time limit), rather
    # than a call to `stop`.
    by_scheduler: bool
    # The job's exit code, if the scheduler said.
    exit_code: int | None = None


class JobEnded(MessageError, RuntimeError):
    """
    The job ended while the code using it ran, or before it was ready: the
    scheduler ended it, or `stop` was called while it started.
    """

    def __init__(self, message: Template, end: JobEnd) -> None:
        super().__init__(message)
        self.end = end


class JobFailed(MessageError, RuntimeError):
    """The job failed; its cause, whose message this is, says how."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(
            cause.message if isinstance(cause, MessageError) else t"{cause}"
        )
        self.__cause__ = cause
