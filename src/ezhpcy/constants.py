import subprocess

from ezhpcy.utils import ezhpcy_version

PACKAGE_NAME = "ezhpcy"
EZHPCY_VERSION = ezhpcy_version()

# The system keyring service that login passwords are stored under, by `user@host`.
KEYRING_SERVICE_NAME = PACKAGE_NAME

# Apps' environments and assets on the cluster: a sibling of the package's
# cache directory, so that pruning stale versions never touches it. Their
# workdirs are in a sibling of the package's state directory of the same name.
APPS_DIRECTORY_NAME = "ezhpcy-apps"


SSH_DIRECTORY_NAME = "ssh"
LOGIN_KNOWN_HOSTS_NAME = "known_hosts"
WORKER_CLIENT_KEY_NAME = "worker_client_ed25519"
WORKER_HOST_ALIAS = "ezhpcy-worker"
WORKER_HOST_KEY_NAME = "ssh_host_ed25519_key"
WORKER_KNOWN_HOSTS_NAME = "worker_known_hosts"
LOOPBACK_HOST = "127.0.0.1"

# How many ports the worker's sshd tries, by default, until one is free.
DEFAULT_WORKER_PORTS = 6

# Keeps helper commands (icacls, whoami, ssh) from flashing a console window on
# Windows; 0 elsewhere.
WINDOWS_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
