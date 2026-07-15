from ezhpcy.utils import ezhpcy_version

PACKAGE_NAME = "ezhpcy"
EZHPCY_VERSION = ezhpcy_version()
SSH_DIRECTORY_NAME = "ssh"
WORKER_CLIENT_KEY_NAME = "worker_client_ed25519"
WORKER_HOST_ALIAS = "ezhpcy-worker"
WORKER_HOST_KEY_NAME = "ssh_host_ed25519_key"

# Vendor software
PIXI_VERSION = "0.73.0"
PIXI_INSTALLER_URL = "https://pixi.sh/install.sh"
OPENSSH_VERSION = "10.4p1"
OPENSSH_MATCHSPEC = f"openssh=={OPENSSH_VERSION}"
