"""
Measure throughput and latency through a running EzHPCy tunnel.

Start `ezhpcy tunnel` first, then run this against the Host alias it serves:

    uv run python scripts/bench_tunnel.py <alias>

It runs plain `ssh <alias> ...` commands, so it measures what a real SSH or VS Code
connection gets, including the `ezhpcy proxy` ProxyCommand. Standard library only.
"""

import argparse
import statistics
import subprocess
import sys
import time

_CHUNK = 1024 * 1024
_WARMUP_ROUNDS = 5


def ssh_command(alias: str, remote_command: str, ssh: str) -> list[str]:
    return [
        ssh,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ControlPath=none",
        alias,
        remote_command,
    ]


def fail(process: subprocess.Popen[bytes], what: str) -> None:
    stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
    sys.exit(f"{what} failed (exit {process.returncode}): {stderr.strip()}")


def download(alias: str, size: int, ssh: str) -> float:
    """MB/s for `size` bytes from the remote's /dev/zero to here."""
    command = ssh_command(alias, f"head -c {size} /dev/zero", ssh)
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        assert process.stdout is not None
        received = 0
        started = time.perf_counter()
        while chunk := process.stdout.read(_CHUNK):
            received += len(chunk)
        elapsed = time.perf_counter() - started
        process.wait()
        if process.returncode != 0:
            fail(process, "download")
    if received != size:
        sys.exit(f"download received {received} of {size} bytes")
    return size / 1e6 / elapsed


def upload(alias: str, size: int, ssh: str) -> float:
    """MB/s for `size` bytes from here to the remote's /dev/null."""
    command = ssh_command(alias, "cat > /dev/null", ssh)
    chunk = bytes(_CHUNK)
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        assert process.stdin is not None
        started = time.perf_counter()
        sent = 0
        while sent < size:
            piece = chunk[: size - sent]
            process.stdin.write(piece)
            sent += len(piece)
        process.stdin.close()
        # `cat` exits once it has read everything, so this waits for the remote
        # to have received it all, not only for the local pipe to drain.
        process.wait()
        elapsed = time.perf_counter() - started
        if process.returncode != 0:
            fail(process, "upload")
    return size / 1e6 / elapsed


def latencies(alias: str, rounds: int, ssh: str) -> list[float]:
    """Milliseconds per line echoed by one remote loop over a single session."""
    command = ssh_command(
        alias, 'while IFS= read -r line; do printf "%s\\n" "$line"; done', ssh
    )
    samples: list[float] = []
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    ) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        # Unbuffered pipes, so each line is one write and each reply one read.
        reader = process.stdout
        for index in range(_WARMUP_ROUNDS + rounds):
            started = time.perf_counter()
            process.stdin.write(b"x\n")
            reply = b""
            while not reply.endswith(b"\n"):
                piece = reader.read(1)
                if not piece:
                    process.wait()
                    fail(process, "latency loop")
                reply += piece
            elapsed = (time.perf_counter() - started) * 1000
            if index >= _WARMUP_ROUNDS:
                samples.append(elapsed)
        process.stdin.close()
        process.wait()
    return samples


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("alias", help="SSH Host alias served by `ezhpcy tunnel`")
    parser.add_argument(
        "--size-mib", type=int, default=1024, help="bytes per transfer (default 1024)"
    )
    parser.add_argument(
        "--rounds", type=int, default=200, help="latency round trips (default 200)"
    )
    parser.add_argument("--ssh", default="ssh", help="ssh executable (default ssh)")
    args = parser.parse_args()
    size = args.size_mib * 1024 * 1024

    print(f"Benchmarking `{args.ssh} {args.alias}`...", file=sys.stderr)
    print("  download...", file=sys.stderr)
    down = download(args.alias, size, args.ssh)
    print("  upload...", file=sys.stderr)
    up = upload(args.alias, size, args.ssh)
    print(f"  {args.rounds} round trips...", file=sys.stderr)
    samples = latencies(args.alias, args.rounds, args.ssh)

    rows = [
        ("Bulk download", f"{down:.1f} MB/s"),
        ("Bulk upload", f"{up:.1f} MB/s"),
        (
            f"Round trip ({args.rounds}), p50 / p95",
            f"{statistics.median(samples):.1f} / {percentile(samples, 0.95):.1f} ms",
        ),
    ]
    width = max(len(name) for name, _ in rows)
    print()
    for name, value in rows:
        print(f"{name:<{width}}  {value}")


if __name__ == "__main__":
    main()
