"""The worker job's command, which runs sshd under the lease watchdog."""

lazy import secrets
lazy import shlex
lazy from pathlib import PurePosixPath

from ezhpcy.worker.sshd import RESOLVE_SSHD
lazy from ezhpcy.constants import OPENSSH_MATCHSPEC
lazy from ezhpcy.types import RemoteState

_FIRST_DYNAMIC_PORT = 49152
_LAST_DYNAMIC_PORT = 65535
_SCRIPT_DELIMITER = "EZHPCY_WORKER_SCRIPT"

_HEARTBEAT_MARKER = "ezhpcy-heartbeat"

# The worker job's shell program. It tries each port in turn until sshd stays up,
# publishes `ready` with that port, then stops sshd once the leases stop.
WORKER_SCRIPT = f"""{RESOLVE_SSHD}
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
    printf '{_HEARTBEAT_MARKER}: %s\n' "$message" >&2
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


def select_ports(count: int) -> tuple[int, ...]:
    """`count` distinct random ports from the dynamic range, for sshd to try in turn."""
    ports: list[int] = []
    while len(ports) < count:
        port = _FIRST_DYNAMIC_PORT + secrets.randbelow(
            _LAST_DYNAMIC_PORT - _FIRST_DYNAMIC_PORT + 1
        )
        if port not in ports:
            ports.append(port)
    return tuple(ports)


def sshd_command(
    remote_state: RemoteState,
    *,
    ports: tuple[int, ...],
    heartbeat_token: str,
    heartbeat_timeout_seconds: int,
    heartbeat_debug: bool,
    control_dir: PurePosixPath,
    read_script_from_stdin: bool,
    sshd_arguments: list[str],
) -> tuple[str, ...]:
    """
    The worker job's command: `WORKER_SCRIPT` in the cached Pixi environment,
    which runs foreground sshd, retrying immediate startup failures by port.

    LSF batch submission sets `read_script_from_stdin` so Bash reads the script
    from `bsub`'s standard input (see `batch_script`). Other jobs run it with
    `bash -c`.
    """
    if not ports:
        raise ValueError("at least one worker SSH port is required")
    script_command = (
        ("bash", "-s", "--")
        if read_script_from_stdin
        else ("bash", "-c", WORKER_SCRIPT, "sshd")
    )
    return remote_state.pixi_command(
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        *script_command,
        ":".join(str(port) for port in ports),
        heartbeat_token,
        str(heartbeat_timeout_seconds),
        "1" if heartbeat_debug else "0",
        str(remote_state.worker_logs_dir()),
        str(control_dir),
        *sshd_arguments,
    )


def batch_script(command: tuple[str, ...], script: str) -> str:
    """Wrap the worker shell program in a batch script fed to LSF over stdin."""
    if f"\n{_SCRIPT_DELIMITER}\n" in script:
        raise ValueError("worker script conflicts with its here-document delimiter")
    return (
        "#!/usr/bin/env bash\n"
        f"exec {shlex.join(command)} <<'{_SCRIPT_DELIMITER}'\n"
        f"{script.rstrip()}\n"
        f"{_SCRIPT_DELIMITER}\n"
    )
