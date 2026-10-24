"""
JupyterLab and TensorBoard on one job's node until Ctrl+C, with the dashboard
started and stopped again beside them, through `ezhpcy.api`: `Config.app` and
`Host.run_app`.

    uv run python examples/api/notebooks.py dtu-hpc --cores 2

The apps are built in, and are provisioned on the node the first time. Open the
printed URLs here.
"""

import argparse
import asyncio
import logging
import signal
import urllib.request
from datetime import timedelta

from ezhpcy import api


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return f"{response.status} {response.reason}"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("profile", help="a profile of the CLI's configuration file")
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--hours", type=float, default=1.0)
    arguments = parser.parse_args()

    config = api.Config.load()
    profile = config.profiles[arguments.profile]
    job_config = profile.job.with_resources(
        cores=arguments.cores, time_limit=timedelta(hours=arguments.hours)
    )

    async with (
        api.connect(profile.login, prompts=api.terminal_prompts()) as login_node,
        login_node.job(job_config) as job,
    ):
        # Ctrl+C ends the job, and `wait` returns; leaving the block cleans up.
        loop = asyncio.get_running_loop()
        signal.signal(
            signal.SIGINT,
            lambda _signum, _frame: loop.call_soon_threadsafe(job.stop, "Ctrl+C"),
        )
        print(f"Job {job.id} on {job.node}")
        # One after another: the second finds the environment they share
        # installed.
        lab = await job.host.run_app(config.app("lab"))
        print(f"JupyterLab: {lab.url}")
        tensorboard = await job.host.run_app(config.app("tensorboard"))
        print(f"TensorBoard: {tensorboard.url}")

        dashboard = await job.host.run_app(config.app("dashboard"))
        status = await asyncio.to_thread(fetch, dashboard.url)
        print(f"Dashboard: {dashboard.url}: {status}")
        await dashboard.stop()
        print("Dashboard stopped; JupyterLab and TensorBoard serve on. Ctrl+C ends.")

        end = await job.wait()
        print(f"Ended: {end.reason}")
    # Leaving the block stops the apps, newest first, and cancels the job.


if __name__ == "__main__":
    # The API logs to the `ezhpcy` logger, and leaves it to the caller to show.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # asyncssh logs a line per channel at INFO, which would drown EzHPCy's own.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    asyncio.run(main())
