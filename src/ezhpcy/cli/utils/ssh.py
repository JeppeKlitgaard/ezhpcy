import logging
import threading
import time
from binascii import hexlify
from collections.abc import Callable

import paramiko
from paramiko.common import DEBUG
from rich.prompt import Confirm, Prompt

from ezhpcy import console
from ezhpcy.config import ConnectionInfo
from ezhpcy.ssh import SSHClient

logger = logging.getLogger(__name__)

_SERVER_ALIVE_REQUEST = "keepalive@openssh.com"
# Matches OpenSSH's ServerAliveCountMax default. With the default 30 second
# interval the login session is declared dead after the same 90 seconds of
# silence that the worker-side heartbeat watchdog allows.
SERVER_ALIVE_COUNT_MAX = 3

ConnectionLostHandler = Callable[[paramiko.SSHException], None]


class PromptMissingHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """
    Prompts the user whether to accept or reject a missing host key.
    If the user accepts, the key is added to the known hosts file.
    """

    def missing_host_key(self, client, hostname, key):
        console.print(
            f"[bold yellow]Warning[/bold yellow]: The host key for [bold purple]{hostname}[/bold purple] is not found in the known hosts file."
        )
        console.print(f"Key type: {key.get_name()}")
        console.print(f"Key fingerprint: {key.get_fingerprint().hex()}")
        user_accepts = Confirm.ask(
            "Do you want to accept this host key?: ",
            console=console,
            default="n",
            case_sensitive=False,
        )
        if user_accepts:
            client._host_keys.add(hostname, key.get_name(), key)

            if client._host_keys_filename is not None:
                client.save_host_keys(client._host_keys_filename)
                client._log(
                    DEBUG,
                    f"Adding {key.get_name()} host key for {hostname}: {hexlify(key.get_fingerprint())}",
                )
            console.print(f"Host key for {hostname} added to known hosts.")
        else:
            raise paramiko.SSHException(f"Host key for {hostname} rejected by user.")


class InteractiveSSHClient(SSHClient):
    conn_info: ConnectionInfo
    password_prompt: bool

    def __init__(
        self,
        conn_info: ConnectionInfo,
        *,
        password_prompt: bool = True,
    ):
        super().__init__(conn_info=conn_info)
        self.conn_info = conn_info
        self.password_prompt = password_prompt
        self._server_alive_stop = threading.Event()
        self._connection_lost_handler: ConnectionLostHandler | None = None

        self.load_system_host_keys()
        self.set_missing_host_key_policy(PromptMissingHostKeyPolicy())

    @staticmethod
    def _can_retry_with_password(error: paramiko.AuthenticationException) -> bool:
        if not isinstance(error, paramiko.BadAuthenticationType):
            return True
        return bool(
            {"password", "keyboard-interactive"}.intersection(error.allowed_types)
        )

    def interactive_connect(self) -> None:
        try:
            self.connect(
                hostname=self.conn_info.host,
                username=self.conn_info.user,
                password=self.conn_info.password,
            )
        except paramiko.AuthenticationException as error:
            if (
                self.conn_info.password is not None
                or not self.password_prompt
                or not self._can_retry_with_password(error)
            ):
                raise

            # Agent and local-key authentication was unavailable or rejected.
            # Reconnect with a prompted password; Paramiko also uses it as the
            # response for single-prompt keyboard-interactive authentication.
            self.close()
            for attempt in range(1, 4):
                password = Prompt.ask(
                    f"Enter password for {self.conn_info.user}@{self.conn_info.host}",
                    password=True,
                    console=console,
                )
                try:
                    self.connect(
                        hostname=self.conn_info.host,
                        username=self.conn_info.user,
                        password=password,
                    )
                    self._enable_keepalive()
                    return
                except paramiko.AuthenticationException as retry_error:
                    self.close()
                    if attempt == 3 or not self._can_retry_with_password(retry_error):
                        raise
                    console.print(
                        f"[bold red]Error[/bold red] [{attempt}/3]: Invalid "
                        "password. Try again or press Ctrl+C to abort."
                    )
        else:
            self._enable_keepalive()

    def set_connection_lost_handler(
        self, handler: ConnectionLostHandler | None
    ) -> None:
        """Register the callback for a login session that stopped answering.

        The keepalive sender always closes the transport itself, so this only
        lets the owning command name the failure and stop immediately instead of
        waiting for some later operation to trip over the dead transport.
        """
        self._connection_lost_handler = handler

    def _report_connection_lost(self, error: paramiko.SSHException) -> None:
        handler = self._connection_lost_handler
        if handler is None:
            return
        try:
            handler(error)
        except Exception:
            logger.exception("Connection-lost handler failed")

    def _enable_keepalive(self) -> None:
        transport = self.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("SSH session is not active")
        interval = self.conn_info.ssh_keepalive_interval_seconds
        # Paramiko's built-in keepalive is a one-way request. Keep it for idle
        # traffic, then add an OpenSSH-style request/reply heartbeat so this has
        # the same liveness properties as ServerAliveInterval.
        transport.set_keepalive(interval)
        self._server_alive_stop = threading.Event()
        threading.Thread(
            target=_send_server_alive_requests,
            args=(
                transport,
                self._server_alive_stop,
                interval,
                self._report_connection_lost,
            ),
            daemon=True,
            name="ezhpcy-ssh-server-alive",
        ).start()

    def close(self) -> None:
        # Stop the keepalive sender before the transport goes away so a
        # deliberate shutdown is never reported as a lost connection.
        self._server_alive_stop.set()
        super().close()


def _request_server_alive(
    transport: paramiko.Transport, *, timeout_seconds: float
) -> bool:
    """Send one keepalive request and report whether the server answered it.

    Paramiko's ``global_request`` waits for a reply without any timeout of its
    own: it returns only once the reply arrives or the transport goes inactive.
    A connection whose packets are being dropped silently would therefore block
    here forever, which is the one outcome a liveness probe must never have. The
    request is issued on a helper thread so an unanswered probe can be counted.
    """
    answered = threading.Event()
    failures: list[Exception] = []

    def request() -> None:
        try:
            # A failure reply still proves that the server is alive, so the
            # response itself is ignored; only silence counts. The request name
            # is the one OpenSSH uses for ServerAliveInterval.
            transport.global_request(_SERVER_ALIVE_REQUEST, wait=True)
        except (EOFError, OSError, paramiko.SSHException) as error:
            failures.append(error)
        finally:
            answered.set()

    threading.Thread(
        target=request,
        daemon=True,
        name="ezhpcy-ssh-server-alive-request",
    ).start()
    if not answered.wait(timeout_seconds):
        return False
    if failures:
        raise failures[0]
    return True


def _end_lost_session(
    transport: paramiko.Transport,
    connection_lost_handler: ConnectionLostHandler | None,
    message: str,
) -> None:
    """Close a login session that can no longer be proven alive."""
    logger.error("%s", message)
    error = paramiko.SSHException(message)
    transport.close()
    if connection_lost_handler is not None:
        connection_lost_handler(error)


def _send_server_alive_requests(
    transport: paramiko.Transport,
    stop_requested: threading.Event,
    interval_seconds: float,
    connection_lost_handler: ConnectionLostHandler | None = None,
    *,
    max_missed_responses: int = SERVER_ALIVE_COUNT_MAX,
) -> None:
    """Probe the server periodically and end a session that stops answering.

    This implements OpenSSH's ServerAliveInterval/ServerAliveCountMax contract.
    A session that cannot be probed is a half-dead session, so every exit from
    this loop other than a requested shutdown closes the transport: continuing
    without a working liveness probe is never an option.
    """
    sequence = 0
    missed_responses = 0
    delay: float = interval_seconds
    while not stop_requested.wait(delay):
        if not transport.is_active():
            _end_lost_session(
                transport,
                connection_lost_handler,
                "Login-node SSH transport went inactive; the session can no "
                f"longer be probed (last server-alive sequence {sequence})",
            )
            return
        sequence += 1
        started = time.monotonic()
        probe_error: Exception | None = None
        try:
            answered = _request_server_alive(
                transport, timeout_seconds=interval_seconds
            )
        except (EOFError, OSError, paramiko.SSHException) as error:
            answered = False
            probe_error = error
        if stop_requested.is_set():
            return
        elapsed = time.monotonic() - started
        # A probe that used a whole interval has already paid for the next one.
        delay = max(0.0, interval_seconds - elapsed)
        # Paramiko also returns from global_request when the transport dies
        # under it, so a completed request only counts while it is still active.
        if answered and transport.is_active():
            missed_responses = 0
            logger.debug(
                "SSH server-alive request answered: sequence=%d elapsed_seconds=%.3f",
                sequence,
                elapsed,
            )
            continue
        if not transport.is_active():
            # Paramiko drops user packets without raising once its transport
            # dies, so this is the usual way a lost connection surfaces here.
            _end_lost_session(
                transport,
                connection_lost_handler,
                f"Login-node SSH transport died during server-alive request "
                f"{sequence}: error={probe_error!r}",
            )
            return
        missed_responses += 1
        if missed_responses < max_missed_responses:
            logger.warning(
                "SSH server-alive request went unanswered: sequence=%d "
                "missed=%d/%d timeout_seconds=%g error=%r",
                sequence,
                missed_responses,
                max_missed_responses,
                interval_seconds,
                probe_error,
            )
            continue
        _end_lost_session(
            transport,
            connection_lost_handler,
            f"Login-node SSH session stopped answering: {missed_responses} "
            f"server-alive requests went unanswered over "
            f"{missed_responses * interval_seconds:g} seconds "
            f"(last error={probe_error!r})",
        )
        return
