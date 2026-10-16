"""Configure the worker's sshd, and run it from the Pixi environment."""

lazy import math
lazy from pathlib import Path, PurePosixPath

# Sets `sshd_path` to the Pixi environment's sshd, which sshd requires to be
# absolute so that it can re-execute itself.
RESOLVE_SSHD = """\
sshd_path="$(command -v sshd)" || exit
case "$sshd_path" in
    /*) ;;
    *) echo "sshd must resolve to an absolute path" >&2; exit 1 ;;
esac
"""


def absolute_sshd_command(arguments: list[str]) -> list[str]:
    """Run ``sshd`` by its resolved absolute path within a Pixi environment."""
    return ["sh", "-c", f'{RESOLVE_SSHD}exec "$sshd_path" "$@"', "sshd", *arguments]


def client_alive(
    heartbeat_interval_seconds: float, heartbeat_timeout_seconds: int
) -> tuple[int, int]:
    """
    Bound how long the worker serves an SSH client that stopped answering.

    A connection that was open when the login session died must end even if
    the lease watchdog cannot signal its handler. The probes reuse the
    heartbeat cadence and give up within the heartbeat timeout.
    """
    interval_seconds = max(1, math.ceil(heartbeat_interval_seconds))
    count_max = max(1, heartbeat_timeout_seconds // interval_seconds)
    return interval_seconds, count_max


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
