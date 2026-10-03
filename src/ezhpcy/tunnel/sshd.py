lazy from pathlib import Path, PurePosixPath

_EXEC_ABSOLUTE_SSHD = (
    'sshd_path="$(command -v sshd)" || exit; '
    'case "$sshd_path" in /*) exec "$sshd_path" "$@";; '
    '*) echo "sshd must resolve to an absolute path" >&2; exit 1;; esac'
)
WORKER_HEARTBEAT_MARKER = "ezhpcy-heartbeat"

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
control_dir="$6"
shift 6
pid=
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

stop_sshd() {{
    # sshd serves each connection from a child that calls setsid(), so neither
    # the listener's exit nor signals to the job's process group reach it. An
    # orphaned connection handler keeps the job alive (and, for interactive
    # jobs, its terminal open), so signal the handlers before the listener.
    if command -v pkill >/dev/null 2>&1; then
        pkill -TERM -P "$pid" 2>/dev/null || true
    else
        log_heartbeat WARN sessions_not_stopped reason=pkill_unavailable
    fi
    kill "$pid" 2>/dev/null || true
}}

stop_children() {{
    if [ -n "$pid" ]; then
        stop_sshd
    fi
    wait "$pid" 2>/dev/null || true
}}

handle_signal() {{
    log_heartbeat INFO worker_stopping reason=signal
    stop_children
    exit 143
}}

publish_state() {{
    state_name="$1"
    shift
    temporary="$control_dir/.$state_name.$$.tmp"
    printf 'v1 %s %s' "$heartbeat_token" "$state_name" > "$temporary" || return
    for field in "$@"; do
        printf ' %s' "$field" >> "$temporary" || return
    done
    printf '\n' >> "$temporary" || return
    mv -f "$temporary" "$control_dir/$state_name"
}}

latest_lease_sequence() {{
    latest=
    for lease in "$control_dir"/lease.*; do
        [ -f "$lease" ] || continue
        IFS=' ' read -r token sequence extra < "$lease" || continue
        case "$sequence" in
            ''|*[!0-9]*) continue ;;
        esac
        if [ "$token" != "$heartbeat_token" ] || [ -n "$extra" ]; then
            continue
        fi
        if [ -z "$latest" ] || [ "$sequence" -gt "$latest" ]; then
            latest="$sequence"
        fi
    done
    printf '%s' "$latest"
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

    publish_state ready "$port" || {{
        log_heartbeat ERROR ready_publish_failed port="$port"
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        exit 77
    }}
    log_heartbeat INFO watchdog_armed timeout_seconds="$heartbeat_timeout" port="$port"

    last_sequence="$(latest_lease_sequence)"
    last_seen=$SECONDS
    lease_exit_status=
    while kill -0 "$pid" 2>/dev/null; do
        sequence="$(latest_lease_sequence)"
        if [ -n "$sequence" ] && {{ [ -z "$last_sequence" ] || [ "$sequence" -gt "$last_sequence" ]; }}; then
            last_sequence="$sequence"
            last_seen=$SECONDS
            if [ "$heartbeat_debug" -eq 1 ]; then
                log_heartbeat DEBUG heartbeat_received sequence="$sequence"
            fi
        fi
        if [ $((SECONDS - last_seen)) -ge "$heartbeat_timeout" ]; then
            lease_exit_status=75
            log_heartbeat ERROR heartbeat_timeout last_sequence="$last_sequence" \
                timeout_seconds="$heartbeat_timeout"
            stop_sshd
            break
        fi
        sleep 1
    done

    wait "$pid"
    sshd_status=$?
    pid=
    if [ -n "$lease_exit_status" ]; then
        log_heartbeat INFO worker_stopped sshd_status="$sshd_status" \
            exit_status="$lease_exit_status"
        publish_state stopped "$lease_exit_status" || true
        exit "$lease_exit_status"
    fi
    log_heartbeat INFO worker_stopped sshd_status="$sshd_status" \
        exit_status="$sshd_status"
    publish_state stopped "$sshd_status" || true
    exit "$sshd_status"
done

publish_state failed no_ports || true
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
    control_dir: PurePosixPath,
    read_script_from_stdin: bool,
) -> list[str]:
    """Run foreground ``sshd``, retrying immediate startup failures by port.

    LSF batch submission sets ``read_script_from_stdin`` so Bash reads the
    worker body from ``bsub`` standard input. Interactive jobs retain the
    directly submitted ``bash -c`` command.
    """
    if not ports:
        raise ValueError("at least one worker SSH port is required")
    script_command = (
        ["bash", "-s", "--"]
        if read_script_from_stdin
        else ["bash", "-c", _EXEC_RETRYING_SSHD, "sshd"]
    )
    return [
        *script_command,
        ":".join(str(port) for port in ports),
        heartbeat_token,
        str(heartbeat_timeout_seconds),
        "1" if heartbeat_debug else "0",
        str(heartbeat_log_dir),
        str(control_dir),
        *arguments,
    ]


def retrying_sshd_script() -> str:
    """Return the shell program invoked by :func:`retrying_sshd_command`."""
    return _EXEC_RETRYING_SSHD


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


def _sshd_config_settings(
    *,
    host_key: PurePosixPath,
    remote_username: str,
    authorized_key: tuple[str, str],
    client_alive: tuple[int, int] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return the complete worker sshd configuration as keyword-value pairs."""
    key_type, key_blob = authorized_key
    liveness: tuple[tuple[str, str], ...] = ()
    if client_alive is not None:
        interval_seconds, count_max = client_alive
        if interval_seconds < 1 or count_max < 1:
            raise ValueError("client-alive interval and count must be positive")
        liveness = (
            ("ClientAliveInterval", str(interval_seconds)),
            ("ClientAliveCountMax", str(count_max)),
        )
    return (
        ("ListenAddress", "0.0.0.0"),
        # File Locations
        ("HostKey", str(host_key)),
        ("AuthorizedKeysFile", "none"),
        ("PidFile", "none"),
        # Authorized Keys
        ("AuthorizedKeysCommand", f"/bin/echo {key_type} {key_blob}"),
        ("AuthorizedKeysCommandUser", remote_username),
        # Authentication Schemes
        ("StrictModes", "yes"),
        ("PubkeyAuthentication", "yes"),
        ("AuthenticationMethods", "publickey"),
        ("PasswordAuthentication", "no"),
        ("KbdInteractiveAuthentication", "no"),
        ("HostbasedAuthentication", "no"),
        ("PermitEmptyPasswords", "no"),
        # Access Control
        ("PermitRootLogin", "no"),
        ("AllowUsers", remote_username),
        # Restrictions
        ("PermitUserEnvironment", "no"),
        ("AllowTcpForwarding", "yes"),
        ("GatewayPorts", "no"),
        ("AllowAgentForwarding", "no"),
        ("X11Forwarding", "no"),
        ("PermitTunnel", "no"),
        # Liveness
        *liveness,
        # Misc
        ("UseDNS", "no"),
        ("LogLevel", "INFO"),
        ("Subsystem", "sftp internal-sftp"),
    )


def sshd_config_arguments(
    *,
    host_key: PurePosixPath,
    remote_username: str,
    authorized_key: tuple[str, str],
    client_alive: tuple[int, int] | None = None,
) -> list[str]:
    """Return the complete worker sshd configuration as command-line options.

    ``client_alive`` is an ``(interval_seconds, count_max)`` pair for sshd's
    ClientAliveInterval and ClientAliveCountMax. The probes travel through the
    tunnel to the real SSH client, so a session whose client became unreachable
    ends on its own instead of outliving the worker.
    """
    settings = _sshd_config_settings(
        host_key=host_key,
        remote_username=remote_username,
        authorized_key=authorized_key,
        client_alive=client_alive,
    )
    return [
        "-f",
        "/dev/null",
        *(part for name, value in settings for part in ("-o", f"{name}={value}")),
    ]
