"""
What the public API raises for asyncssh's errors, so that its callers never need
asyncssh. Inside the package, asyncssh's own errors are caught where they mean
something; what is left becomes an `SSHError` at the API's edge.
"""

from functools import wraps
lazy from collections.abc import Awaitable, Callable, Coroutine
lazy from typing import Any

lazy import asyncssh

from ezhpcy.messages import MessageError


class SSHError(MessageError, RuntimeError):
    """
    The host refused something, or its connection failed, as SSH reported it,
    such as a path SFTP can't read or a port the host won't listen on.
    """


def translating_ssh_errors[**P, R](
    call: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """
    Make `call` raise `SSHError` for asyncssh's errors, a refused listen included,
    which isn't an `asyncssh.Error`.
    """

    @wraps(call)
    async def translated(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await call(*args, **kwargs)
        except (asyncssh.Error, asyncssh.ChannelListenError) as error:
            raise SSHError(t"{error}") from error

    return translated
