#!/usr/bin/env bash
set -euo pipefail

UV_MATCHSPEC="uv==0.11.26"

xdg_data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
install_root="${EZHPCY_INSTALL_ROOT:-$xdg_data_home/ezhpcy}"
pixi_home="${PIXI_HOME:-$install_root/pixi_home}"
pixi_bin="$pixi_home/bin/pixi"

run_pixi() {
    PIXI_HOME="$pixi_home" \
        PIXI_NO_PATH_UPDATE="1" \
        "$pixi_bin" "$@"
}

if [[ ! -x "$pixi_bin" ]]; then
    echo "ezhpcy's private Pixi installation was not found at: $pixi_bin" >&2
    exit 1
fi

echo "Uninstalling ezhpcy..."
run_pixi exec --spec="$UV_MATCHSPEC" uv tool uninstall ezhpcy

echo "Removing ezhpcy installation files from: $install_root"
# On NFS, deleting this running script creates a temporary .nfs* file until
# Bash closes it. Replacing Bash with rm closes the script before cleanup.
exec rm -rf -- "$install_root"
