#!/usr/bin/env bash
set -euo pipefail

xdg_data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
xdg_cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
install_root="${EZHPCY_INSTALL_ROOT:-$xdg_data_home/ezhpcy}"
ezhpcy_cache_dir="${EZHPCY_CACHE_DIR:-$xdg_cache_home/ezhpcy}"
pixi_home="${PIXI_HOME:-$install_root/pixi_home}"
pixi_cache_dir="${PIXI_CACHE_DIR:-$ezhpcy_cache_dir/pixi_cache}"
pixi_bin="$pixi_home/bin/pixi"
uv_matchspec="${1:?Usage: uninstall.sh UV_MATCHSPEC}"

run_pixi() {
    PIXI_HOME="$pixi_home" \
        PIXI_CACHE_DIR="$pixi_cache_dir" \
        PIXI_NO_PATH_UPDATE="1" \
        "$pixi_bin" "$@"
}

if [[ ! -x "$pixi_bin" ]]; then
    echo "ezhpcy's private Pixi installation was not found at: $pixi_bin" >&2
    exit 1
fi

echo "Uninstalling ezhpcy..."
run_pixi exec --spec="$uv_matchspec" uv tool uninstall ezhpcy

echo "Removing ezhpcy's cache: ${ezhpcy_cache_dir}"
rm -rf -- "$ezhpcy_cache_dir"

echo "Removing ezhpcy installation files from: $install_root"
# On NFS, deleting this running script creates a temporary .nfs* file until
# Bash closes it. Replacing Bash with rm closes the script before cleanup.
exec rm -rf -- "$install_root"
