"""
What is attached to a running job: a Host block, an app, a forwarded port.

Each is an `Attachment`, made by `JobLifecycle.attach`, which registers it on the
job's exit stack. As it acquires things, it pushes the cleanup step for each, so
that its cleanup covers exactly what it acquired, in reverse order, each step
with a timeout. It is closed once: early, by `close`, or when the job is left,
whichever comes first; the other then finds it closed, or waits for it.

Its `status`, if it reports one, is in the job's, until it closes.
"""

lazy import asyncio
lazy from collections.abc import Awaitable, Callable
lazy from contextlib import AsyncExitStack

lazy from ezhpcy.job.lifecycle import cleanup
lazy from ezhpcy.job.status import AttachmentStatus


class Attachment:
    """
    Something attached to a job, with its cleanup steps. `on_change` is called
    when its status changes, and when it closes.
    """

    def __init__(self, on_change: Callable[[], object]) -> None:
        self._stack = AsyncExitStack()
        self._closing: asyncio.Task[None] | None = None
        self._on_change = on_change
        self.status: AttachmentStatus | None = None

    def report(self, status: AttachmentStatus) -> None:
        """Replace the attachment's status."""
        self.status = status
        self._on_change()

    def push(
        self,
        description: str,
        step: Callable[[], Awaitable[object]],
        *,
        seconds: float | None = None,
    ) -> None:
        """
        Run `step` when the attachment is closed, before the steps pushed before
        it, as a cleanup step (see `cleanup`).
        """
        if self._closing is not None:
            raise RuntimeError("the attachment is closed")
        self._stack.push_async_callback(cleanup, description, step, seconds=seconds)

    @property
    def closed(self) -> bool:
        """Whether the attachment is closed, or closing."""
        return self._closing is not None

    async def close(self) -> None:
        """
        Run the cleanup steps, the newest first, unless they ran already; then
        return once they have. Cancelled, the steps still run to the end.
        """
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._stack.aclose())
            self._on_change()
        await asyncio.shield(self._closing)
