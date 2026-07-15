from pathlib import Path

from ezhpcy.config import LocalFileConfig


def test_local_file_config_ssh_dir_requires_machine_id() -> None:
    local_files = LocalFileConfig(config_dir=Path("config"))

    assert local_files.ssh_dir("machine-id") == Path("config/ssh/machine-id")
