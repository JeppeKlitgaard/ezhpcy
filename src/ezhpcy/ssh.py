import os

import paramiko


class MissingHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    pass

def get_ssh_client() -> paramiko.SSHClient:
    """
    Create and return an SSH client connected to the DTU HPC login node.
    """
    ssh_client = paramiko.SSHClient()
    ssh_client.load_system_host_keys()
    ssh_client.set_missing_host_key_policy(MissingHostKeyPolicy())
    ssh_client.connect("login.hpc.dtu.dk", username=os.environ.get("USER"))
    return ssh_client
