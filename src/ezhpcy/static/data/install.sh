#!/usr/bin/env bash
set -euo pipefail

UV_MATCHSPEC="uv==0.11.26"
OPENSSH_MATCHSPEC="openssh"

pixi_install_script_url="https://pixi.sh/install.sh"
xdg_data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
install_root="${EZHPCY_INSTALL_ROOT:-$xdg_data_home/ezhpcy}"
pixi_home="${PIXI_HOME:-$install_root/pixi_home}"
pixi_bin="$pixi_home/bin/pixi"
pixi_install_script="$install_root/pixi-install.sh"
sdist_path="${1:-}"

download_file() {
    local url="$1"
    local output_path="$2"

    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --show-error --silent "$url" --output "$output_path"
        return
    fi

    if command -v wget >/dev/null 2>&1; then
        wget --quiet --output-document="$output_path" "$url"
        return
    fi

    echo "Neither curl nor wget is available to download $url" >&2
    exit 1
}

### pixi
install_pixi() {
    if [[ -x "$pixi_bin" ]]; then
        echo "Pixi is already installed at: $pixi_bin"
        return
    fi

    mkdir -p "$install_root" "$pixi_home"
    download_file "$pixi_install_script_url" "$pixi_install_script"
    chmod +x "$pixi_install_script"

    PIXI_HOME="$pixi_home" \
        PIXI_NO_PATH_UPDATE="1" \
        bash "$pixi_install_script"
}

run_pixi() {
    PIXI_HOME="$pixi_home" \
        "$pixi_bin" "$@"
}

### sdist of ezhpcy
mkdir -p "$install_root"

if [[ -z "$sdist_path" ]]; then
    sdist_path="$(find "$install_root" -maxdepth 1 -type f -name 'ezhpcy-*.tar.gz' -print -quit)"
fi

if [[ -z "$sdist_path" || ! -f "$sdist_path" ]]; then
    echo "No bundled ezhpcy sdist found in $install_root" >&2
    exit 1
fi

install_pixi
run_pixi --version

# Materialize OpenSSH on shared storage so worker jobs never need to download it.
echo "Installing the worker OpenSSH environment..."
run_pixi global install "$OPENSSH_MATCHSPEC"
"$pixi_home/bin/sshd" -V

echo "Bundled ezhpcy sdist staged at: $sdist_path"

# Install ezhpcy from the sdist using pixi
echo "Installing ezhpcy from sdist..."
run_pixi exec --spec="$UV_MATCHSPEC" uv tool install "$sdist_path" --no-config --force-reinstall

# Validate installation
echo "Validating ezhpcy installation..."
echo "ezhpcy version: $(ezhpcy version)"
