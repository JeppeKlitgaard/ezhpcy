from pathlib import Path

from ezhpcy.config import LocalFileConfig
from ezhpcy.utils import ssh_connection_id


def test_local_file_config_ssh_dir_scopes_by_machine_user_and_host() -> None:
    local_files = LocalFileConfig(config_dir=Path("config"))

    assert local_files.ssh_dir(
        "machine-id", user="alice", host="LOGIN2.HPC.DTU.DK."
    ) == Path("config/ssh/machine-id/alice@login2.hpc.dtu.dk")


def test_ssh_connection_id_escapes_path_separators_and_user_at_signs() -> None:
    assert (
        ssh_connection_id(r"domain/alice@example", "login.example.com")
        == "domain%2Falice%40example@login.example.com"
    )
