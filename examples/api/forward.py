"""
A web server started on a job's node, forwarded here, fetched, then stopped,
through `ezhpcy.api`: `Job.host`, `Host.start` and `Host.forward`.

    uv run python examples/api/forward.py dtu-hpc --cores 1

The server is Python's own `http.server`, on the node's loopback.
"""

import argparse
import asyncio
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from ezhpcy import api


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return f"{response.status} {response.reason}"


async def fetch_when_ready(url: str) -> str:
    """Fetch `url` once the server answers, as it takes a moment to start."""
    async with asyncio.timeout(60):
        while True:
            try:
                return await asyncio.to_thread(fetch, url)
            except urllib.error.URLError, ConnectionError:
                await asyncio.sleep(1)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("profile", help="a profile of the CLI's configuration file")
    parser.add_argument("--cores", type=int, default=1)
    parser.add_argument("--hours", type=float, default=0.5)
    parser.add_argument("--port", type=int, default=8765, help="on the node")
    arguments = parser.parse_args()

    profile = api.Config.load().profiles[arguments.profile]
    job_config = profile.job.with_resources(
        cores=arguments.cores, time_limit=timedelta(hours=arguments.hours)
    )
    port = str(arguments.port)

    async with (
        api.connect(profile.login, prompts=api.terminal_prompts()) as login_node,
        login_node.job(job_config) as job,
    ):
        node = job.host
        print(f"Job {job.id} on {(await node.run(['hostname'])).strip()}")
        server = await node.start(
            ["python3", "-m", "http.server", "--bind", "127.0.0.1", port]
        )
        mode = await node.run(["stat", "-c", "%a %U", str(server.run_dir)])
        print(f"Started in {server.run_dir} ({mode.strip()})")
        forward = await node.forward(api.TCPDestination("127.0.0.1", arguments.port))
        url = f"http://127.0.0.1:{forward.port}/"
        print(f"Fetched {url}: {await fetch_when_ready(url)}")
        await server.stop()
        print(f"Stopped, with exit status {await server.wait()}")
    # Leaving the blocks would have stopped the server too, and cancels the job.


if __name__ == "__main__":
    # The API logs to the `ezhpcy` logger, and leaves it to the caller to show.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # asyncssh logs a line per channel at INFO, which would drown EzHPCy's own.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    asyncio.run(main())
