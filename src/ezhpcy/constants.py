import subprocess

from ezhpcy.utils import ezhpcy_version

PACKAGE_NAME = "ezhpcy"
EZHPCY_VERSION = ezhpcy_version()
SSH_DIRECTORY_NAME = "ssh"
LOGIN_KNOWN_HOSTS_NAME = "known_hosts"
WORKER_CLIENT_KEY_NAME = "worker_client_ed25519"
WORKER_HOST_ALIAS = "ezhpcy-worker"
WORKER_HOST_KEY_NAME = "ssh_host_ed25519_key"

# Keeps helper commands (icacls, whoami, ssh) from flashing a console window on
# Windows; 0 elsewhere.
WINDOWS_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Vendor software
PIXI_VERSION = "0.81.0"
PIXI_INSTALLER_URL = "https://pixi.sh/install.sh"
OPENSSH_VERSION = "10.5p1"
OPENSSH_MATCHSPEC = f"openssh=={OPENSSH_VERSION}"
