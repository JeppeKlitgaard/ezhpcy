"""
A cluster's queues as JSON, as each profile's scheduler describes them to its
user, through `ezhpcy.api`: `Host.queues`.

    uv run python examples/api/queues.py dtu-a100sh dtu-linuxsh > queues.json

DTU schedules some queues with LSF and others with PBS Pro, hence a profile of
each. Profiles with the same login node share its connection. A profile's
`site.wrapper_directories` say where the site's submission wrappers are.
"""

import argparse
import asyncio
import json
import logging
from contextlib import AsyncExitStack

from ezhpcy import api


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "profiles", nargs="+", help="profiles of the CLI's configuration file"
    )
    parser.add_argument(
        "--available", action="store_true", help="only queues that would run my jobs"
    )
    arguments = parser.parse_args()

    config = api.Config.load()
    clusters = []
    async with AsyncExitStack() as stack:
        hosts: dict[api.Login, api.Host] = {}
        for name in arguments.profiles:
            profile = config.profiles[name]
            if profile.login not in hosts:
                hosts[profile.login] = await stack.enter_async_context(
                    api.connect(profile.login, prompts=api.terminal_prompts())
                )
            cluster = await hosts[profile.login].queues(
                profile.job,
                site=profile.site,
                only_available_to_me=arguments.available,
            )
            clusters.append(cluster.model_dump(mode="json", exclude_none=True))
    print(json.dumps(clusters, indent=2))


if __name__ == "__main__":
    # The API logs to the `ezhpcy` logger, and leaves it to the caller to show.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(main())
