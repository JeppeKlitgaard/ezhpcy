"""
An app on an SSH server that isn't a cluster, until Ctrl+C, through `ezhpcy.api`:
connected to directly, with no job.

    uv run python examples/api/workstation.py workstation.example.com --user alice
    uv run python examples/api/workstation.py gpu-box --user alice --app tensorboard

The app is provisioned on the server the first time, by default under
`ezhpcy-apps` in its XDG cache directory, which needs a Linux x86-64 server with
the internet. Open the printed URL here. Nothing stops the app if this script is
killed; Ctrl+C, or the app's own exit, does.
"""

import argparse
import asyncio
import logging
from pathlib import PurePosixPath

from ezhpcy import api


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("host", help="the SSH server: a domain name or an IP address")
    parser.add_argument("--user", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument(
        "--app", default="lab", help="a built-in app, as NAME or NAME@VERSION"
    )
    parser.add_argument("--apps-root", type=PurePosixPath, help="on the server")
    arguments = parser.parse_args()

    config = api.Config.load()
    name, _, version = arguments.app.partition("@")
    app = config.app(name, version or None)
    login = api.Login(host=arguments.host, user=arguments.user, port=arguments.port)

    async with api.connect(login, prompts=api.terminal_prompts()) as host:
        running = await host.run_app(app, apps_root=arguments.apps_root)
        print(f"{running.app}: {running.url}")
        # Until the app exits. Ctrl+C cancels this, and leaving the block stops
        # the app and removes its run directory.
        status = await running.wait()
        print(f"{running.app} exited with status {status}:")
        print(await running.stderr())


if __name__ == "__main__":
    # The API logs to the `ezhpcy` logger, and leaves it to the caller to show.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # asyncssh logs a line per channel at INFO, which would drown EzHPCy's own.
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    asyncio.run(main())
