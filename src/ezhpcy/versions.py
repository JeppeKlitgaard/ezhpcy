"""
Versions of software and specifications used by EzHPCy.

These are frequently bumped with new releases of EzHPCy.
"""

# The layout of an apps root's installs.
# Bump when EzHPCy app layouts change.
# The Pixi version is also keyed in the apps root, so the layout versions
# DOES NOT need to be bumped when the Pixi version changes.
# If a Pixi version bump breaks the format of Pixi (rattler) cache, then it
# can be fixed by bumping the app layout version. This should be exceedingly rare.
# This is important especially for shared app roots on clusters where
# we need to support a wider range of EzHPCy versions and we don't want to break
# older versions.
# The apps are indexed according to this version, allowing different app layouts of the
# same app versions to coexist in the same apps root.
APPS_LAYOUT_VERSION = 1

# Vendor software
# See versions: https://github.com/prefix-dev/pixi
PIXI_VERSION = "0.82.1"
PIXI_INSTALLER_URL = "https://pixi.sh/install.sh"

# See versions: https://anaconda.org/channels/conda-forge/packages/openssh/files
OPENSSH_VERSION = "10.5p1"
OPENSSH_MATCHSPEC = f"openssh=={OPENSSH_VERSION}"
