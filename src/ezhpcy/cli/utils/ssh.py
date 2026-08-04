import logging
import threading
from binascii import hexlify
from pathlib import Path, PurePosixPath

import paramiko
from paramiko.common import DEBUG
from rich.prompt import Confirm, Prompt

from ezhpcy import console
from ezhpcy.config import ConnectionInfo
from ezhpcy.ssh import SSHClient

logger = logging.getLogger(__name__)

_EXEC_ABSOLUTE_SSHD = (
    'sshd_path="$(command -v sshd)" || exit; '
    'case "$sshd_path" in /*) exec "$sshd_path" "$@";; '
    '*) echo "sshd must resolve to an absolute path" >&2; exit 1;; esac'
)
SSHD_PORT_MARKER = "ezhpcy: worker sshd selected port "
WORKER_HEARTBEAT_MARKER = "ezhpcy-heartbeat"
_SERVER_ALIVE_REQUEST = "keepalive@openssh.com"
_EXEC_RETRYING_SSHD = f"""
sshd_path="$(command -v sshd)" || exit
case "$sshd_path" in
    /*) ;;
    *) echo "sshd must resolve to an absolute path" >&2; exit 1 ;;
esac

ports="$1"
heartbeat_token="$2"
heartbeat_timeout="$3"
heartbeat_debug="$4"
heartbeat_log_dir="$5"
shift 5
pid=
tty_echo_disabled=0
job_id="${{LSB_JOBID:-${{PBS_JOBID:-unknown-$$}}}}"
safe_job_id="${{job_id//[^A-Za-z0-9_.-]/_}}"
heartbeat_log="$heartbeat_log_dir/heartbeat-$safe_job_id.log"

umask 077
mkdir -p "$heartbeat_log_dir" || exit

log_heartbeat() {{
    level="$1"
    event="$2"
    shift 2
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    message="$timestamp level=$level component=worker-heartbeat event=$event job=$job_id"
    if [ "$#" -gt 0 ]; then
        message="$message $*"
    fi
    printf '%s\n' "$message" >> "$heartbeat_log"
    printf '{WORKER_HEARTBEAT_MARKER}: %s\n' "$message" >&2
}}

restore_tty() {{
    if [ "$tty_echo_disabled" -eq 1 ]; then
        stty echo 2>/dev/null || true
        tty_echo_disabled=0
    fi
}}

stop_children() {{
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
    restore_tty
}}

handle_signal() {{
    log_heartbeat INFO worker_stopping reason=signal
    stop_children
    exit 143
}}

trap handle_signal HUP INT TERM

while [ -n "$ports" ]; do
    case "$ports" in
        *:*) port="${{ports%%:*}}"; ports="${{ports#*:}}" ;;
        *) port="$ports"; ports= ;;
    esac

    "$sshd_path" -D -e -p "$port" "$@" &
    pid=$!
    sleep 0.1
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid"
        pid=
        continue
    fi

    if [ -t 0 ] && stty -echo 2>/dev/null; then
        tty_echo_disabled=1
    fi
    printf "{SSHD_PORT_MARKER}%s\n" "$port"
    log_heartbeat INFO watchdog_armed timeout_seconds="$heartbeat_timeout" port="$port"

    last_sequence=none
    lease_exit_status=
    while kill -0 "$pid" 2>/dev/null; do
        marker=
        received_token=
        sequence=
        extra=
        IFS=' ' read -r -t "$heartbeat_timeout" \
            marker received_token sequence extra
        read_status=$?
        if [ "$read_status" -ne 0 ]; then
            if ! kill -0 "$pid" 2>/dev/null; then
                break
            fi
            if [ "$read_status" -gt 128 ]; then
                event=heartbeat_timeout
                lease_exit_status=75
            else
                event=stdin_eof
                lease_exit_status=74
            fi
            log_heartbeat ERROR "$event" last_sequence="$last_sequence" \
                timeout_seconds="$heartbeat_timeout"
            kill "$pid" 2>/dev/null || true
            break
        fi
        if [ "$marker" != "{WORKER_HEARTBEAT_MARKER}" ] || \
            [ "$received_token" != "$heartbeat_token" ] || \
            [ -z "$sequence" ] || [ -n "$extra" ]; then
            log_heartbeat ERROR invalid_heartbeat last_sequence="$last_sequence"
            lease_exit_status=76
            kill "$pid" 2>/dev/null || true
            break
        fi
        last_sequence="$sequence"
        if [ "$heartbeat_debug" -eq 1 ]; then
            log_heartbeat DEBUG heartbeat_received sequence="$sequence"
        fi
    done

    wait "$pid"
    sshd_status=$?
    pid=
    restore_tty
    if [ -n "$lease_exit_status" ]; then
        log_heartbeat INFO worker_stopped sshd_status="$sshd_status" \
            exit_status="$lease_exit_status"
        exit "$lease_exit_status"
    fi
    log_heartbeat INFO worker_stopped sshd_status="$sshd_status" \
        exit_status="$sshd_status"
    exit "$sshd_status"
done

exit 1
"""


def absolute_sshd_command(arguments: list[str]) -> list[str]:
    """Run ``sshd`` by its resolved absolute path within a Pixi environment."""
    return ["sh", "-c", _EXEC_ABSOLUTE_SSHD, "sshd", *arguments]


def retrying_sshd_command(
    arguments: list[str],
    ports: tuple[int, ...],
    *,
    heartbeat_token: str,
    heartbeat_timeout_seconds: float,
    heartbeat_debug: bool,
    heartbeat_log_dir: PurePosixPath,
) -> list[str]:
    """Run foreground ``sshd``, retrying immediate startup failures by port."""
    if not ports:
        raise ValueError("at least one worker SSH port is required")
    return [
        "bash",
        "-c",
        _EXEC_RETRYING_SSHD,
        "sshd",
        ":".join(str(port) for port in ports),
        heartbeat_token,
        str(heartbeat_timeout_seconds),
        "1" if heartbeat_debug else "0",
        str(heartbeat_log_dir),
        *arguments,
    ]


def read_ed25519_public_key(path: Path) -> tuple[str, str]:
    """Read the two fields needed to authorize a generated worker key."""
    try:
        fields = path.read_text(encoding="ascii").split()
    except (OSError, UnicodeError) as error:
        raise RuntimeError(
            f"Could not read worker public key at {path}: {error}"
        ) from error
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise RuntimeError(f"The worker public key at {path} is malformed.")
    key_type, key_blob = fields[:2]
    if not key_blob or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
        for character in key_blob
    ):
        raise RuntimeError(f"The worker public key at {path} is malformed.")
    return key_type, key_blob


def sshd_config_arguments(
    *,
    host_key: PurePosixPath,
    remote_username: str,
    authorized_key: tuple[str, str],
) -> list[str]:
    """Return the complete worker sshd configuration as command-line options."""
    key_type, key_blob = authorized_key
    settings = (
        "ListenAddress=0.0.0.0",
        # File Locations
        f"HostKey={host_key}",
        "AuthorizedKeysFile=none",
        "PidFile=none",
        # Authorized Keys
        f"AuthorizedKeysCommand=/bin/echo {key_type} {key_blob}",
        f"AuthorizedKeysCommandUser={remote_username}",
        # Authentication Schemes
        "StrictModes=yes",
        "PubkeyAuthentication=yes",
        "AuthenticationMethods=publickey",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "HostbasedAuthentication=no",
        "PermitEmptyPasswords=no",
        # Access Control
        "PermitRootLogin=no",
        f"AllowUsers={remote_username}",
        # Restrictions
        "PermitUserEnvironment=no",
        "AllowTcpForwarding=yes",
        "GatewayPorts=no",
        "AllowAgentForwarding=no",
        "X11Forwarding=no",
        "PermitTunnel=no",
        # Misc
        "UseDNS=no",
        "LogLevel=INFO",
        "Subsystem=sftp internal-sftp",
    )
    return [
        "-f",
        "/dev/null",
        *(part for setting in settings for part in ("-o", setting)),
    ]


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
                    "Adding {} host key for {}: {}".format(
                        key.get_name(), hostname, hexlify(key.get_fingerprint())
                    ),
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
            args=(transport, self._server_alive_stop, interval),
            daemon=True,
            name="ezhpcy-ssh-server-alive",
        ).start()

    def close(self) -> None:
        self._server_alive_stop.set()
        super().close()


def _send_server_alive_requests(
    transport: paramiko.Transport,
    stop_requested: threading.Event,
    interval_seconds: int,
) -> None:
    """Request a server response periodically, matching OpenSSH keepalives."""
    while not stop_requested.wait(interval_seconds):
        if not transport.is_active():
            logger.debug("SSH server-alive sender stopped: transport inactive")
            return
        try:
            # A failure response still proves that the server is alive. The
            # request name is the one OpenSSH uses for ServerAliveInterval.
            transport.global_request(_SERVER_ALIVE_REQUEST, wait=True)
            logger.debug("SSH server-alive request completed")
        except (EOFError, OSError, paramiko.SSHException) as error:
            logger.debug(
                "SSH server-alive request failed: transport_active=%s error=%r",
                transport.is_active(),
                error,
            )
            return
