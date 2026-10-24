"""
A worker job published under an alias until Ctrl+C, through `ezhpcy.api`: what
`ezhpcy tunnel PROFILE --alias ALIAS` does, without its status display.

    uv run python examples/api/tunnel.py dtu-hpc --alias gpu --cores 4

Then, in another terminal, `ssh gpu` reaches the job's node.
"""

import argparse
import asyncio
import logging
import signal
from datetime import timedelta

from ezhpcy import api


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("profile", help="a profile of the CLI's configuration file")
    parser.add_argument("--alias", help="the alias; by default the profile's name")
    parser.add_argument("--cores", type=int, default=1)
    parser.add_argument("--hours", type=float, default=1.0)
    arguments = parser.parse_args()

    profile = api.Config.load().profiles[arguments.profile]
    job_config = profile.job.with_resources(
        cores=arguments.cores, time_limit=timedelta(hours=arguments.hours)
    )
    alias = arguments.alias or profile.name

    async with (
        api.reserve_alias(alias) as reservation,
        api.connect(profile.login, prompts=api.terminal_prompts()) as login_node,
        login_node.job(job_config) as job,
    ):
        # Ctrl+C ends the job, and `wait` returns; leaving the block cleans up.
        loop = asyncio.get_running_loop()
        signal.signal(
            signal.SIGINT,
            lambda _signum, _frame: loop.call_soon_threadsafe(job.stop, "Ctrl+C"),
        )
        published = await job.publish(reservation)
        print(f"Job {job.id} on {job.node}: ssh {published.alias}")
        end = await job.wait()
        print(f"Ended: {end.reason}")


if __name__ == "__main__":
    # The API logs to the `ezhpcy` logger, and leaves it to the caller to show.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # asyncssh logs a line per channel at INFO, which would drown EzHPCy's own.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    asyncio.run(main())
