"""
Versions of software and specifications used by EzHPCy.

These are frequently bumped with new releases of EzHPCy.
"""

# Vendor software
# See versions: https://github.com/prefix-dev/pixi
PIXI_VERSION = "0.82.1"
PIXI_INSTALLER_URL = "https://pixi.sh/install.sh"

# See versions: https://anaconda.org/channels/conda-forge/packages/openssh/files
OPENSSH_VERSION = "10.5p1"
OPENSSH_MATCHSPEC = f"openssh=={OPENSSH_VERSION}"
