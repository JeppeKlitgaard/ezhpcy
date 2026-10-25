"""
Steps that acquire something, and the cleanup steps that undo them, for a job,
a host's attachments, or anything else that must never leave something behind:
`finish` lets a step that acquires something finish even when cancelled, and
`cleanup` runs a step with a timeout, warning rather than raising if it fails.
"""

lazy import asyncio
lazy from collections.abc import Awaitable, Callable

from ezhpcy.logging import get_logger

logger = get_logger(__name__)

# How long each cleanup step may take, so that a dead connection can't hang it.
_CLEANUP_SECONDS = 10.0


async def finish[T](
    description: str,
    step: Awaitable[T],
    *,
    then: Callable[[T], object] | None = None,
) -> T:
    """
    Await `step`, then call `then` with its result, e.g. to register a cleanup
    step for what `step` acquired.

    Cancelled midway, a step may still take effect (a job queued by a `bsub`
    whose output never arrived, a file written by a thread), with nothing to
    undo it. So if the caller is cancelled meanwhile, `step` still finishes
    first, within the cleanup timeout, and `then` is called; only then does the
    cancellation proceed.
    """
    task = asyncio.ensure_future(step)
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.debug(t"Waiting to {description} before stopping.")
        done, _ = await asyncio.wait([task], timeout=_CLEANUP_SECONDS)
        if not done:
            task.cancel()
            logger.warning(
                t"Gave up waiting to {description} after "
                t"{_CLEANUP_SECONDS:detail:g} seconds; it may have left "
                t"something behind."
            )
        # A step that failed acquired nothing, and the caller stops anyway.
        elif task.exception() is None and then is not None:
            then(task.result())
        raise
    if then is not None:
        then(result)
    return result


async def cleanup(
    description: str,
    step: Callable[[], Awaitable[object]],
    *,
    seconds: float | None = None,
) -> None:
    """
    Run one cleanup step, for at most `seconds` (by default `_CLEANUP_SECONDS`);
    if it fails, warn and carry on.
    """
    seconds = _CLEANUP_SECONDS if seconds is None else seconds
    try:
        async with asyncio.timeout(seconds):
            await step()
    except TimeoutError:
        logger.warning(
            t"Could not {description}: no answer within {seconds:detail:g} seconds."
        )
    except Exception as error:
        logger.warning(
            t"Could not {description}: {error}",
            exc_info=True,
        )
