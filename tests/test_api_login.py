"""
`ezhpcy.api` before a job: configuration files, connecting with and without
prompts, alias reservations and the worker's credentials. Uses `ezhpcy.api`
alone, with the in-process SSH server.
"""

import asyncio
from datetime import timedelta
from pathlib import Path

import asyncssh
import pytest
from pydantic import SecretStr

from ezhpcy import api
from tests.support.login import offer_client_key
from tests.support.sshserver import SSHTestServer
from tests.support.tunnel import openssh_resolves


class Answers:
    """Prompts that answer from what the test sets, and record what was asked."""

    def __init__(self, *, password: str = "secret", accept: bool = True) -> None:
        self.password = password
        self.accept = accept
        self.asked: list[tuple[str, ...]] = []

    async def ask_password(self, user: str, host: str, attempt: int) -> str:
        self.asked.append(("password", user, host, str(attempt)))
        return self.password

    async def accept_host_key(
        self, host: str, port: int, key_type: str, fingerprint: str
    ) -> bool:
        self.asked.append(("host key", host, str(port), key_type, fingerprint))
        return self.accept

    def prompts(self) -> api.Prompts:
        return api.Prompts(
            ask_password=self.ask_password, accept_host_key=self.accept_host_key
        )


@pytest.fixture
def server(
    ssh_server: SSHTestServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> SSHTestServer:
    """The test server, offered its client key, with its host key unknown."""
    offer_client_key(ssh_server, monkeypatch, tmp_path / "home")
    return ssh_server


def login_to(server: SSHTestServer, **settings: object) -> api.Login:
    return api.Login.model_validate(
        {"host": server.host, "port": server.port, "user": server.user, **settings}
    )


def known_hosts_file() -> Path:
    """EzHPCy's own known hosts file, in the test's local files."""
    return api.Config.from_mapping({}).local_file.login_known_hosts_file


def trust(server: SSHTestServer) -> Path:
    """Put the server's host key in EzHPCy's known hosts file; return the file."""
    known_hosts = known_hosts_file()
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.write_text(server.known_hosts_line, encoding="utf-8")
    return known_hosts


def only_passwords(server: SSHTestServer, password: str = "secret") -> None:
    server.auth.client_keys = [asyncssh.generate_private_key("ssh-ed25519")]
    server.auth.password = password


async def test_key_authentication_asks_nothing(server: SSHTestServer) -> None:
    trust(server)
    answers = Answers()

    async with api.connect(login_to(server), prompts=answers.prompts()) as cluster:
        assert cluster.login.user == server.user

    assert answers.asked == []


async def test_without_prompts_a_password_the_server_wants_fails_saying_so(
    server: SSHTestServer,
) -> None:
    trust(server)
    only_passwords(server)

    with pytest.raises(api.ConnectionFailed, match="password"):
        async with api.connect(login_to(server)):
            pytest.fail("connected")


async def test_without_prompts_an_unknown_host_key_fails_saying_so(
    server: SSHTestServer,
) -> None:
    with pytest.raises(api.ConnectionFailed, match="no prompt to accept it"):
        async with api.connect(login_to(server)):
            pytest.fail("connected")


async def test_a_password_is_asked_for_once_keys_fail(server: SSHTestServer) -> None:
    trust(server)
    only_passwords(server)
    answers = Answers()

    async with api.connect(login_to(server), prompts=answers.prompts()):
        pass

    assert answers.asked == [("password", server.user, server.host, "1")]


async def test_a_password_given_is_tried_once_and_never_asked_for(
    server: SSHTestServer,
) -> None:
    trust(server)
    only_passwords(server, "right")
    answers = Answers()

    with pytest.raises(api.ConnectionFailed):
        async with api.connect(
            login_to(server), prompts=answers.prompts(), password=SecretStr("wrong")
        ):
            pass

    assert answers.asked == []
    assert [
        accepted for method, accepted in server.auth_attempts if method == "password"
    ] == [False]


async def test_password_prompt_false_never_asks(server: SSHTestServer) -> None:
    trust(server)
    only_passwords(server)
    answers = Answers()

    with pytest.raises(api.ConnectionFailed):
        async with api.connect(
            login_to(server, password_prompt=False), prompts=answers.prompts()
        ):
            pass

    assert answers.asked == []


async def test_an_accepted_host_key_is_saved(server: SSHTestServer) -> None:
    answers = Answers(accept=True)

    async with api.connect(login_to(server), prompts=answers.prompts()):
        pass
    # Known now: not asked again.
    async with api.connect(login_to(server), prompts=answers.prompts()):
        pass

    [(what, host, port, key_type, fingerprint)] = answers.asked
    assert (what, host, port) == ("host key", server.host, str(server.port))
    assert key_type == "ssh-ed25519"
    assert fingerprint == server.host_key.get_fingerprint()
    saved = known_hosts_file().read_text(encoding="utf-8")
    assert saved.strip() == server.known_hosts_line.strip()


async def test_a_rejected_host_key_fails_and_is_not_saved(
    server: SSHTestServer,
) -> None:
    answers = Answers(accept=False)

    with pytest.raises(api.ConnectionFailed, match="rejected"):
        async with api.connect(login_to(server), prompts=answers.prompts()):
            pass

    assert not known_hosts_file().exists()


async def test_a_changed_host_key_is_refused_without_asking(
    server: SSHTestServer,
) -> None:
    other = asyncssh.generate_private_key("ssh-ed25519")
    key_type, key_data, *_ = other.export_public_key("openssh").decode().split()
    known_hosts = trust(server)
    known_hosts.write_text(
        f"[{server.host}]:{server.port} {key_type} {key_data}\n", encoding="utf-8"
    )
    answers = Answers()

    with pytest.raises(api.ConnectionFailed, match="does not match"):
        async with api.connect(login_to(server), prompts=answers.prompts()):
            pass

    assert answers.asked == []


async def test_the_cluster_says_when_its_connection_ends(
    server: SSHTestServer,
) -> None:
    trust(server)

    async with api.connect(login_to(server)) as cluster:
        server.drop()
        async with asyncio.timeout(5):
            error = await cluster.closed

    assert error is not None


CONFIG = """
auto_provision = false

[connection]
password_keyring = true

[profile.base]
description = "The base"
connection.host = "login.example.com"
connection.user = "alice"
scheduler.type = "LSF"
scheduler.submission_mode = "batch"
resources.queue = "hpc"
resources.time_limit = "2:00"
timings.queue_timeout = 60

[profile.gpu]
inherit = "base"
resources.gpus = 1

[profile.unfinished]
connection.user = "alice"
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "ezhpcy.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_a_loaded_configuration_resolves_its_profiles(config_file: Path) -> None:
    loaded = api.Config.load(config_file)

    gpu = loaded.profiles["gpu"]
    assert (gpu.name, gpu.description) == ("gpu", "The base")
    assert gpu.login == api.Login(
        host="login.example.com", user="alice", password_keyring=True
    )
    job = gpu.job
    assert job.scheduler.type is api.SchedulerType.LSF
    assert job.scheduler.submission_mode is api.SubmissionMode.BATCH
    assert (job.resources.queue, job.resources.gpus) == ("hpc", 1)
    assert job.resources.time_limit == timedelta(hours=2)
    assert job.timings.queue_timeout == timedelta(seconds=60)
    assert job.auto_provision is False
    assert loaded.auto_provision is False


def test_a_profile_without_a_required_setting_says_which(config_file: Path) -> None:
    unfinished = api.Config.load(config_file).profiles["unfinished"]

    with pytest.raises(api.ConfigError, match=r"profile\.unfinished\.connection\.host"):
        unfinished.login  # noqa: B018
    with pytest.raises(api.ConfigError, match=r"profile\.unfinished\.scheduler\.type"):
        unfinished.job  # noqa: B018


def test_a_missing_or_invalid_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(api.ConfigError, match="Could not read"):
        api.Config.load(tmp_path / "missing.toml")
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[profile.base]\nresources.cores = 0\n", encoding="utf-8")
    with pytest.raises(api.ConfigError, match=r"profile\.base\.resources\.cores"):
        api.Config.load(invalid)


def test_models_are_built_directly_and_changed_by_validated_copies() -> None:
    job = api.JobConfig(
        scheduler=api.SchedulerSettings(
            type=api.SchedulerType.LSF, submission_mode=api.SubmissionMode.BATCH
        ),
        resources=api.Resources(queue="hpc"),
    )

    changed = job.with_resources(cores=4, time_limit=timedelta(hours=2)).with_timings(
        queue_timeout=timedelta(minutes=5)
    )

    assert (changed.resources.queue, changed.resources.cores) == ("hpc", 4)
    assert changed.timings.queue_timeout == timedelta(minutes=5)
    assert job.resources.cores == 1
    with pytest.raises(api.ConfigError, match=r"resources\.cores"):
        job.with_resources(cores=0)
    with pytest.raises(api.ConfigError, match="whole number of minutes"):
        job.with_resources(time_limit=timedelta(seconds=90))


def test_an_interactive_submission_command_refuses_what_it_would_ignore() -> None:
    job = api.JobConfig(
        scheduler=api.SchedulerSettings(
            type=api.SchedulerType.LSF,
            submission_mode=api.SubmissionMode.INTERACTIVE,
            interactive_submission_command=("a100sh",),
        )
    )

    with pytest.raises(api.ConfigError, match=r"resources\.cores"):
        job.with_resources(cores=4)
    with pytest.raises(api.ConfigError, match="interactive"):
        job.with_scheduler(submission_mode=api.SubmissionMode.BATCH)


def test_models_are_frozen() -> None:
    login = api.Login(host="login.example.com", user="alice")

    with pytest.raises(ValueError, match="frozen"):
        login.user = "bob"  # type: ignore[misc]


async def test_a_held_alias_cant_be_reserved_again() -> None:
    async with api.reserve_alias("gpu") as reservation:
        assert reservation.alias == "gpu"
        with pytest.raises(api.AliasInUse, match="already running for gpu"):
            async with api.reserve_alias("gpu"):
                pytest.fail("reserved twice")

    # Released with its block.
    async with api.reserve_alias("gpu"):
        pass


async def test_an_alias_ssh_would_not_reach_is_not_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the Include, OpenSSH takes the alias for the host name.
    openssh_resolves(monkeypatch, lambda alias: {"hostname": [alias]})

    with pytest.raises(
        api.AliasUnreachable, match="OpenSSH resolves it to gpu"
    ) as caught:
        async with api.reserve_alias("gpu"):
            pytest.fail("reserved")

    # Nothing is printed: the message points where the lines are.
    assert "below" not in str(caught.value)
    assert "ezhpcy doctor" in str(caught.value)
    assert caught.value.problem.include_lines is not None
    assert "Include" in caught.value.problem.include_lines
    assert api.probe_alias("gpu") == caught.value.problem


async def test_a_name_that_cant_be_an_alias_is_a_config_error() -> None:
    with pytest.raises(api.ConfigError, match="may contain only"):
        async with api.reserve_alias("two words"):
            pytest.fail("reserved")


def test_missing_credentials_names_the_worker_keys_to_provision() -> None:
    missing = api.missing_credentials(api.Login(host="login.example.com", user="alice"))

    assert missing
    assert all(isinstance(path, Path) for path in missing)
