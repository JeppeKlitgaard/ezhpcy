"""
A login shell for the batch of commands a scheduler adapter runs at once
(`Scheduler._run_all`): its script's commands, answered from fixed responses.
"""

import re
import shlex
from collections.abc import Mapping

from ezhpcy.types import Login

# A command's exit status, stdout and stderr; or its stdout, for one that works.
type Response = str | tuple[int, str, str]


def answer_batch(
    script: str,
    responses: Mapping[str, Response],
    commands: list[list[str]] | None = None,
) -> str:
    """
    What the login shell prints for `script`: each command it runs gets the
    response of the first pattern in `responses` that its words match, or fails
    as a command not found. Each command's words are appended to `commands`.
    """
    [marker] = set(re.findall(r"(ezhpcy-[0-9a-f]+)", script))
    output = []
    for line in re.findall(
        r"^\{ (.*?); echo \$\? > ", script, re.MULTILINE | re.DOTALL
    ):
        command = shlex.split(line)
        if commands is not None:
            commands.append(command)
        status, stdout, stderr = _respond(shlex.join(command), responses)
        output.append(f"\n{marker} {status}\n{stdout}\n{marker}-stderr\n{stderr}")
    return "".join(output)


def _respond(command: str, responses: Mapping[str, Response]) -> tuple[int, str, str]:
    for pattern, response in responses.items():
        if re.search(pattern, command):
            return (0, response, "") if isinstance(response, str) else response
    return 127, "", f"{command.split(maxsplit=1)[0]}: command not found"


class BatchShell:
    """
    Stands in for `RemoteHost`, for an adapter: its login shell runs the batch
    with `answer_batch`, recording the commands in `commands`.
    """

    def __init__(self, responses: Mapping[str, Response]) -> None:
        self.login = Login(host="login.example.org", user="alice")
        self.responses = responses
        self.commands: list[list[str]] = []

    async def run_login_shell(
        self, args: list[str], *, stdin: str | None = None
    ) -> str:
        [script] = args[2:]
        return answer_batch(script, self.responses, self.commands)
