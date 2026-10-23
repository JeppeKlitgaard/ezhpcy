"""The worker job's command, which runs sshd under the lease watchdog."""

import shlex
lazy import secrets
lazy from pathlib import PurePosixPath

from ezhpcy.worker.sshd import RESOLVE_SSHD
lazy from ezhpcy.constants import OPENSSH_MATCHSPEC
lazy from ezhpcy.types import RemoteState

_FIRST_DYNAMIC_PORT = 49152
_LAST_DYNAMIC_PORT = 65535

_HEARTBEAT_MARKER = "ezhpcy-heartbeat"
# How long the app may take to exit after SIGTERM before it gets SIGKILL.
APP_STOP_GRACE_SECONDS = 10

# The launch files the session writes into the control directory before it submits
# a job with an app (see `ezhpcy.app.launch`): the app's script, and the shell
# assignments the worker reads (`run_dir`, `app_ports`).
APP_SCRIPT_NAME = "app.sh"
RUN_ENV_NAME = "run.env"

# Shell functions for both programs below, given `heartbeat_log`, `job_id`, sshd's
# `pid` and the app's `app_pid` (empty without an app), which leads its own
# process group.
_SHARED_FUNCTIONS = f"""\
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

stop_app() {{
    # The app's whole process group, which holds what it started too (such as
    # Jupyter's kernels): politely, then for good.
    [ -n "$app_pid" ] || return 0
    kill -TERM -- "-$app_pid" 2>/dev/null || return 0
    waited=0
    while kill -0 -- "-$app_pid" 2>/dev/null && [ "$waited" -lt {APP_STOP_GRACE_SECONDS} ]; do
        sleep 1
        waited=$((waited + 1))
    done
    kill -KILL -- "-$app_pid" 2>/dev/null || true
}}
"""

# The lease watchdog, which the worker job starts in a session of its own. Once
# the client of an interactive job's submission is gone, the scheduler kills the
# job's terminal session (LSF within a second, sshd with it), but not processes in
# other sessions, such as those SSH clients started through sshd, or the app, and
# they keep the job running. So once sshd or the app has exited, or no newer lease
# has appeared within the heartbeat timeout, the watchdog cancels the job through
# the scheduler, which ends every process in it; failing that, it stops sshd and
# the app.
WATCHDOG_SCRIPT = f"""{_SHARED_FUNCTIONS}
heartbeat_token="$1"
heartbeat_timeout="$2"
heartbeat_log="$3"
control_dir="$4"
job_id="$5"
cancel_executable="$6"
pid="$7"
app_pid="${{8:-}}"

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

children_live() {{
    kill -0 "$pid" 2>/dev/null &&
        {{ [ -z "$app_pid" ] || kill -0 "$app_pid" 2>/dev/null; }}
}}

reason=
last_sequence="$(latest_lease_sequence)"
last_seen=$SECONDS
while children_live; do
    sequence="$(latest_lease_sequence)"
    if [ -n "$sequence" ] && {{ [ -z "$last_sequence" ] || [ "$sequence" -gt "$last_sequence" ]; }}; then
        last_sequence="$sequence"
        last_seen=$SECONDS
        log_heartbeat DEBUG heartbeat_received sequence="$sequence"
    fi
    if [ $((SECONDS - last_seen)) -ge "$heartbeat_timeout" ]; then
        reason=heartbeat_timeout
        log_heartbeat ERROR heartbeat_timeout last_sequence="$last_sequence" \
            timeout_seconds="$heartbeat_timeout"
        break
    fi
    sleep 1
done
if [ -z "$reason" ]; then
    if kill -0 "$pid" 2>/dev/null; then
        reason=app_exited
    else
        reason=sshd_exited
    fi
fi

output="$("$cancel_executable" "$job_id" 2>&1)"
status=$?
if [ "$status" -eq 0 ]; then
    log_heartbeat INFO job_cancelled reason="$reason" output="${{output//$'\n'/ }}"
else
    log_heartbeat ERROR job_cancel_failed reason="$reason" exit_status="$status" \
        output="${{output//$'\n'/ }}"
    if [ "$reason" = heartbeat_timeout ]; then
        stop_sshd
        stop_app
    fi
fi
"""

# The worker job's shell program. It tries each port in turn until sshd stays up,
# starts the app if the session wrote its launch files, publishes its status
# `ready` with sshd's port (and the app's endpoint), then starts the lease
# watchdog. Once sshd or the app exits, it publishes `stopped` with which one and
# its exit status, stops the other, and removes the app's run directory.
WORKER_SCRIPT = f"""{RESOLVE_SSHD}
ports="$1"
heartbeat_token="$2"
heartbeat_timeout="$3"
heartbeat_log_dir="$4"
control_dir="$5"
cancel_executable="$6"
shift 6
pid=
app_pid=
app_endpoint=
run_dir=
created_run_dir=
job_id="${{LSB_JOBID:-${{PBS_JOBID:-unknown-$$}}}}"
safe_job_id="${{job_id//[^A-Za-z0-9_.-]/_}}"
heartbeat_log="$heartbeat_log_dir/heartbeat-$safe_job_id.log"
watchdog_script={shlex.quote(WATCHDOG_SCRIPT)}

umask 077
mkdir -p "$heartbeat_log_dir" || exit

{_SHARED_FUNCTIONS}
remove_run_dir() {{
    # Only the one this worker created: another's may be at a path it was given.
    if [ -n "$created_run_dir" ]; then
        rm -rf -- "$run_dir"
        created_run_dir=
    fi
}}

stop_children() {{
    if [ -n "$pid" ]; then
        stop_sshd
    fi
    stop_app
    wait "$pid" 2>/dev/null || true
    if [ -n "$app_pid" ]; then
        wait "$app_pid" 2>/dev/null || true
    fi
    remove_run_dir
}}

# Sets `app_port` to the first of `app_ports` that nothing listens on, on
# 127.0.0.1.
pick_app_port() {{
    candidates="$app_ports"
    while [ -n "$candidates" ]; do
        case "$candidates" in
            *:*) candidate="${{candidates%%:*}}"; candidates="${{candidates#*:}}" ;;
            *) candidate="$candidates"; candidates= ;;
        esac
        if ! (exec 3<>"/dev/tcp/127.0.0.1/$candidate") 2>/dev/null; then
            app_port="$candidate"
            return 0
        fi
    done
    return 1
}}

# Starts the app from the launch files, in a process group of its own, setting
# `app_pid` and `app_endpoint`: `socket`, or the TCP port it must listen on.
start_app() {{
    . "$control_dir/{RUN_ENV_NAME}" || return
    app_endpoint=socket
    if [ -n "$app_ports" ]; then
        pick_app_port || {{
            log_heartbeat ERROR app_start_failed reason=no_app_ports
            return 1
        }}
        app_endpoint="$app_port"
    fi
    # Private, and refused if the path exists: it may be someone else's.
    mkdir -m 700 -- "$run_dir" || {{
        log_heartbeat ERROR app_start_failed reason=run_dir_exists
        return 1
    }}
    created_run_dir=1
    app_log="$heartbeat_log_dir/app-$heartbeat_token"
    # setsid execs without forking, as a background job isn't a process group
    # leader, so `app_pid` leads the app's process group.
    setsid "$BASH" "$control_dir/{APP_SCRIPT_NAME}" "$app_endpoint" \
        < /dev/null > "$app_log.out" 2> "$app_log.err" &
    app_pid=$!
    log_heartbeat INFO app_started pid="$app_pid" endpoint="$app_endpoint"
}}

handle_signal() {{
    log_heartbeat INFO worker_stopping reason=signal
    stop_children
    exit 143
}}

publish_state() {{
    state_name="$1"
    shift
    temporary="$control_dir/.status.$$.tmp"
    printf '%s %s' "$heartbeat_token" "$state_name" > "$temporary" || return
    for field in "$@"; do
        printf ' %s' "$field" >> "$temporary" || return
    done
    printf '\n' >> "$temporary" || return
    mv -f "$temporary" "$control_dir/status"
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

    if [ -f "$control_dir/{APP_SCRIPT_NAME}" ]; then
        start_app || {{
            stop_children
            publish_state failed app_start_failed || true
            exit 1
        }}
    fi
    publish_state ready "$port" ${{app_endpoint:+"$app_endpoint"}} || {{
        log_heartbeat ERROR ready_publish_failed port="$port"
        stop_children
        exit 77
    }}
    # Forked by setsid rather than started with `&`, which would have it ignore
    # SIGINT, the first signal a cancelled LSF job gets. Without the job's
    # terminal, so that it neither holds it open nor can block on it.
    setsid -f "$BASH" -c "$watchdog_script" watchdog "$heartbeat_token" \
        "$heartbeat_timeout" "$heartbeat_log" "$control_dir" "$job_id" \
        "$cancel_executable" "$pid" ${{app_pid:+"$app_pid"}} \
        < /dev/null > /dev/null 2>&1 || {{
        log_heartbeat ERROR watchdog_start_failed
        stop_children
        publish_state failed watchdog_start_failed || true
        exit 1
    }}
    log_heartbeat INFO watchdog_armed timeout_seconds="$heartbeat_timeout" port="$port"

    # Until sshd or the app exits: they are the only children.
    wait -n
    exit_status=$?
    if [ -n "$app_pid" ] && ! kill -0 "$app_pid" 2>/dev/null; then
        stopped=app
    else
        stopped=sshd
        pid=
    fi
    log_heartbeat INFO worker_stopped process="$stopped" exit_status="$exit_status"
    # Before stopping the other, which may take a while: the session reads it.
    publish_state stopped "$stopped" "$exit_status" || true
    stop_children
    exit "$exit_status"
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
    control_dir: PurePosixPath,
    cancel_executable: str,
    sshd_arguments: list[str],
) -> tuple[str, ...]:
    """
    The worker job's command: `WORKER_SCRIPT` in the cached Pixi environment,
    which runs foreground sshd, retrying immediate startup failures by port.
    Its watchdog cancels the job with `cancel_executable`.
    """
    if not ports:
        raise ValueError("at least one worker SSH port is required")
    return remote_state.pixi_command(
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        "bash",
        "-c",
        WORKER_SCRIPT,
        "sshd",
        ":".join(str(port) for port in ports),
        heartbeat_token,
        str(heartbeat_timeout_seconds),
        str(remote_state.worker_logs_dir()),
        str(control_dir),
        cancel_executable,
        *sshd_arguments,
    )
